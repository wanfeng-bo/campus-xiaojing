"""Offline contract for the isolated, deliberately unprovisioned live stack."""

from pathlib import Path
import re

import yaml
from tests.test_dingtalk_test_deployment_assets import extract_nginx_block


ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "infra/docker/compose.school-facts-production.yml"
GATEWAY = ROOT / "infra/nginx/school-facts-production.conf.template"
WEB_IMAGE = ROOT / "infra/docker/web.school-fact-live.Dockerfile"
PUBLIC_EXAMPLE = ROOT / "infra/docker/school-facts-production.env.example"


def test_live_stack_is_independent_and_does_not_claim_old_public_ports():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = document["services"]
    assert set(services) == {"postgres", "redis", "api", "writer", "web"}
    assert not any(spec.get("ports") for name, spec in services.items() if name != "web")
    assert services["web"]["ports"] == [
        "127.0.0.1:${CAMPUS_AI_LIVE_HTTP_PORT:?free HTTP port required}:80",
        "127.0.0.1:${CAMPUS_AI_LIVE_HTTPS_PORT:?free HTTPS port required}:443",
    ]
    assert "external" not in str(document["volumes"])
    assert set(document["volumes"]) == {"postgres_data", "redis_data"}
    assert all(spec.get("pull_policy") == "never" for spec in services.values())
    assert all(spec.get("restart") == "unless-stopped" for spec in services.values())


def test_live_roles_and_configuration_are_separate():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert services["api"]["environment"]["SCHOOL_FACT_LIVE_CONFIG"] == "/run/school-facts-live/product.json"
    assert services["writer"]["environment"]["SCHOOL_FACT_F1_WRITER_CONFIG_FILE"] == "/run/school-facts-live/writer.json"
    assert services["writer"]["command"][:2] == ["uvicorn", "app.composition.school_fact_product_writer:create_school_fact_product_writer_app"]
    assert services["api"]["healthcheck"]["test"][-1].find("/api/v1/school-fact-product/features") >= 0
    assert services["writer"]["healthcheck"]["test"][-1].find("/internal/v1/school-facts/features") >= 0
    assert "campus_ai_identity_test" not in COMPOSE.read_text(encoding="utf-8")
    assert "20260824_20" not in COMPOSE.read_text(encoding="utf-8")
    for name in ("api", "writer"):
        assert services[name]["user"] == "10001:10001"
        assert services[name]["read_only"] is True
        assert services[name]["cap_drop"] == ["ALL"]
        assert not services[name].get("ports")
        assert not any("docker.sock" in str(volume) for volume in services[name]["volumes"])


def test_live_gateway_exposes_only_product_and_preserves_headers():
    gateway = GATEWAY.read_text(encoding="utf-8")
    for path in ("/api/v1/admin", "/internal/", "/api/v1/health"):
        assert path in gateway
    assert "return 404;" in gateway
    assert "proxy_pass http://api:8000;" in gateway
    assert 'add_header Cache-Control "no-store" always;' in gateway
    assert 'add_header X-Robots-Tag "noindex, nofollow" always;' in gateway
    assert "limit_req zone=dingtalk_login_per_ip" in gateway


def test_gateway_accepts_the_approved_ten_mib_base64_json_source():
    gateway = GATEWAY.read_text(encoding="utf-8")
    upload = extract_nginx_block(
        gateway, "location = /api/v1/school-fact-product/imports/sources"
    )
    match = re.search(r"client_max_body_size (\d+)m;", upload)
    assert match is not None
    largest_source = 10 * 1024 * 1024
    base64_length = 4 * ((largest_source + 2) // 3)
    # HTTP upload has one bounded 255-character filename and two JSON keys.
    largest_json_body = base64_length + 255 + 100
    assert int(match.group(1)) * 1024 * 1024 > largest_json_body


def test_assistant_stream_has_its_own_bounded_idle_timeout():
    gateway = GATEWAY.read_text(encoding="utf-8")
    assistant = extract_nginx_block(gateway, "location ^~ /api/v1/assistant/")
    assert "proxy_pass http://api:8000;" in assistant
    assert "proxy_read_timeout 210s;" in assistant
    assert "proxy_buffering off;" in assistant
    login = extract_nginx_block(
        gateway, "location = /api/v1/school-fact-product/dingtalk/exchange"
    )
    assert "proxy_read_timeout 15s;" in login


def test_synchronous_writer_routes_outwait_the_existing_sixty_second_client_bound():
    gateway = GATEWAY.read_text(encoding="utf-8")
    match = re.search(r"location ~ (\^/api/v1/school-fact-product/[^\s]+) \{", gateway)
    assert match is not None
    block = extract_nginx_block(gateway, match.group(0)[:-2].strip())
    assert "proxy_read_timeout 75s;" in block
    assert "proxy_pass http://api:8000;" in block
    pattern = re.compile(match.group(1))
    for path in (
        "/api/v1/school-fact-product/contacts/prepare",
        "/api/v1/school-fact-product/contacts/proposals/opaque/confirm",
        "/api/v1/school-fact-product/facts/prepare",
        "/api/v1/school-fact-product/family-facts/proposals/opaque/confirm",
        "/api/v1/school-fact-product/education-facts/prepare",
        "/api/v1/school-fact-product/residence-facts/proposals/opaque/confirm",
        "/api/v1/school-fact-product/family/corrections",
        "/api/v1/school-fact-product/family-facts/corrections/opaque/confirm-review",
        "/api/v1/school-fact-product/imports/plans/package",
        "/api/v1/school-fact-product/imports/plans/opaque/confirm",
    ):
        assert pattern.fullmatch(path), path
    for path in (
        "/api/v1/school-fact-product/dingtalk/exchange",
        "/api/v1/school-fact-product/contacts/query",
        "/api/v1/school-fact-product/imports/sources",
        "/api/v1/school-fact-product/features",
    ):
        assert not pattern.fullmatch(path), path


def test_public_example_has_no_real_ids_or_secret_values_and_web_is_distinct():
    example = PUBLIC_EXAMPLE.read_text(encoding="utf-8")
    assert "CAMPUS_AI_LIVE_BIND_IP" not in example
    assert "CAMPUS_AI_LIVE_HTTP_PORT=18080" in example
    assert "CAMPUS_AI_LIVE_HTTPS_PORT=18443" in example
    assert "DINGTALK_CORP_ID=" not in example
    assert "DINGTALK_APP_KEY=" not in example
    assert "PASSWORD=" not in example
    dockerfile = WEB_IMAGE.read_text(encoding="utf-8")
    assert "school-facts-production.conf.template" in dockerfile
    assert "VITE_DINGTALK_CORP_ID" in dockerfile
    assert "VITE_DINGTALK_CLIENT_ID" in dockerfile
    assert "npm run build:dingtalk-school-facts" in dockerfile
    assert "dist-dingtalk-school-facts" in dockerfile
    assert "dingtalk-school-facts.html /usr/share/nginx/html/index.html" in dockerfile
    formal_entry = (ROOT / "apps/web/dingtalk-school-facts.html").read_text(encoding="utf-8")
    formal_main = (ROOT / "apps/web/src/dingtalk_school_facts_main.ts").read_text(encoding="utf-8")
    assert "src/dingtalk_school_facts_main.ts" in formal_entry
    assert 'import DingTalkSchoolFactsApp from "./app/DingTalkSchoolFactsApp.vue"' in formal_main
