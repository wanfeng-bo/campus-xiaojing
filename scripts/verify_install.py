"""No model calls. Optional PG16 verification creates and removes ONLY its own disposable cluster."""
from __future__ import annotations
import argparse, json, os, secrets, subprocess, sys, time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

def local_endpoint(value):
    return value.startswith(("npipe:////./pipe/", "unix:///")) and "\n" not in value

def owned(labels, token):
    return bool(token) and labels.get("purpose")=="this-task" and labels.get("release-check")==token

def run(args, *, env=None, cwd=None, timeout=40):
    return subprocess.run(args,env=env,cwd=cwd,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout)

def docker(*args, env=None, timeout=40): return run(["docker",*args],env=env,timeout=timeout)

def container_state():
    ids=docker("ps","-aq")
    if ids.returncode: raise RuntimeError("Docker inventory unavailable")
    result={}
    for identity in ids.stdout.split():
        item=docker("inspect","--format","{{.Id}} {{.State.StartedAt}} {{.State.Status}}",identity)
        if item.returncode: raise RuntimeError("Docker inventory changed while inspecting")
        parts=item.stdout.split();result[parts[0]]=parts[1:]
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--run-disposable-database",action="store_true");args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];report_dir=root/"runtime/install-verification";report_dir.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy()
    for key in list(env):
        if key.startswith(("SCHOOL_FACT_","RUN_","POSTGRES_","DINGTALK_","DEEPSEEK_","MASTER_DATA_")): env.pop(key)
    env.pop("PYTHONPATH",None);env["PYTHONUTF8"]="1"
    check=run([sys.executable,"-m","pip","check"],env=env,cwd=report_dir)
    imports=run([sys.executable,"-c","import app.composition.school_fact_live_product, app.composition.school_fact_product_writer; import pydantic_ai; print('formal imports ok')"],env=env,cwd=report_dir)
    report={"pip_check":check.returncode,"formal_imports":imports.returncode,"paid_model_calls":0,"real_school_data_used":False}
    if check.returncode or imports.returncode:
        report["status"]="dependency_failure";(report_dir/"summary.json").write_text(json.dumps(report,indent=2),encoding="utf-8");print(json.dumps(report));return 1
    if not args.run_disposable_database:
        report["database"]="not_requested";print(json.dumps(report));return 0
    token=uuid4().hex[:12];name="school-facts-live-integration-tests-opensource-"+token;network="campus-opensource-check-"+token
    endpoint=os.environ.get("DOCKER_HOST", "")
    if not endpoint:
        context=docker("context","inspect","--format","{{.Endpoints.docker.Host}}")
        if context.returncode:raise RuntimeError("Cannot verify Docker context")
        endpoint=context.stdout.strip()
    if not local_endpoint(endpoint):raise RuntimeError("Only a local Docker socket or named pipe is allowed for this verification")
    previous=container_state();created_container=False;created_network=False;container_id=None;started=time.monotonic()
    report.update(container=name,network=network)
    try:
        if docker("image","inspect","postgres:16").returncode: raise RuntimeError("Local postgres:16 image required; pull explicitly before verification")
        made=docker("network","create","--label","purpose=this-task","--label","release-check="+token,network)
        if made.returncode:raise RuntimeError("Could not create isolated test network")
        created_network=True
        password=secrets.token_urlsafe(40);db="opensource_install_test"
        boot_env=env|{"POSTGRES_PASSWORD":password,"POSTGRES_USER":"postgres","POSTGRES_DB":db}
        launched=docker("run","-d","--name",name,"--label","purpose=this-task","--label","release-check="+token,"--network",network,"--publish","127.0.0.1::5432","--tmpfs","/var/lib/postgresql/data:rw","-e","POSTGRES_PASSWORD","-e","POSTGRES_USER","-e","POSTGRES_DB","postgres:16",env=boot_env)
        if launched.returncode:raise RuntimeError("Could not create disposable PostgreSQL")
        created_container=True;container_id=launched.stdout.strip()
        for _ in range(30):
            if docker("exec",name,"pg_isready","-U","postgres","-d",db,timeout=10).returncode==0:break
            time.sleep(1)
        else:raise RuntimeError("Disposable PostgreSQL did not become ready")
        port_result=docker("inspect","--format","{{json .NetworkSettings.Ports}}",name)
        bindings=json.loads(port_result.stdout)["5432/tcp"]
        report["port_bindings"]=bindings
        if len(bindings)!=1 or bindings[0]["HostIp"]!="127.0.0.1":raise RuntimeError("Unexpected test port binding")
        system=docker("exec",name,"psql","-U","postgres","-d",db,"-Atqc","SELECT system_identifier::text FROM pg_control_system()")
        if system.returncode or not system.stdout.strip().isdigit():raise RuntimeError("Cannot verify disposable cluster identity")
        env.update(APP_ENV="test",POSTGRES_HOST="127.0.0.1",POSTGRES_PORT=bindings[0]["HostPort"],POSTGRES_DB=db,POSTGRES_BOOTSTRAP_USER="postgres",POSTGRES_BOOTSTRAP_PASSWORD=password,MASTER_DATA_DISPOSABLE_CLUSTER_SYSTEM_IDENTIFIER=system.stdout.strip(),DINGTALK_TEST_POSTGRES_CONTAINER=name,RUN_DINGTALK_POSTGRES_INTEGRATION="1")
        nodes=["tests/test_live_school_scope_writer_postgres.py::test_live_school_scope_writer_read_update_and_revoke","tests/test_live_school_scope_postgres.py::test_live_school_scope_metadata_guards_and_complete_keyset","tests/school_facts/test_live_scope_migration.py"]
        xml=report_dir/"results.xml"
        result=run([sys.executable,"-m","pytest",*nodes,"-q","--tb=no","-p","no:cacheprovider","--junitxml",str(xml)],env=env,cwd=root/"apps/api",timeout=420)
        report["pytest_exit_code"]=result.returncode
        if xml.exists():
            tree=ET.parse(xml);cases=[]
            for case in tree.iter("testcase"):
                status="failed" if case.find("failure") is not None or case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
                cases.append({"name":case.get("name"),"status":status,"seconds":case.get("time")})
            report["cases"]=cases
            # Drop failure text and environment metadata before keeping the result.
            for elem in tree.iter():
                if elem.tag in {"failure","error","system-out","system-err","property"}:elem.clear()
            tree.write(xml,encoding="utf-8")
        report["status"]="passed" if result.returncode==0 and report.get("cases") and all(c["status"]=="passed" for c in report["cases"]) else "failed"
    except Exception as exc:
        report["status"]="failed";report["failure_type"]=type(exc).__name__
        # Known static messages only; never echo captured subprocess output or settings.
        if type(exc) is RuntimeError: report["reason"]=str(exc)
    finally:
        if created_container:
            proof=docker("inspect","--format","{{json .Config.Labels}}",container_id)
            if proof.returncode==0 and owned(json.loads(proof.stdout) or {},token):
                report["container_cleanup"]=docker("rm","-f",container_id).returncode==0
            else:report["container_cleanup"]=False
        if created_network:
            proof=docker("network","inspect","--format","{{json .Labels}}",network)
            if proof.returncode==0 and owned(json.loads(proof.stdout) or {},token):report["network_cleanup"]=docker("network","rm",network).returncode==0
            else:report["network_cleanup"]=False
        try:report["other_containers_unchanged"]=container_state()==previous
        except Exception:report["other_containers_unchanged"]=False
        report["seconds"]=round(time.monotonic()-started,2)
        (report_dir/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps(report,ensure_ascii=False))
    return 0 if report["status"]=="passed" and report.get("container_cleanup") and report.get("network_cleanup") and report.get("other_containers_unchanged") else 1

if __name__=="__main__":raise SystemExit(main())
