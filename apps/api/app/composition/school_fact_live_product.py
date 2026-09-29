"""Explicit public DingTalk product; never falls back to the engineering entry."""

from fastapi import FastAPI

from app.composition.school_fact_live_settings import load_school_fact_live_configuration
from app.composition.school_fact_product import create_school_fact_product_app


def create_app() -> FastAPI:
    return create_school_fact_product_app(configuration=load_school_fact_live_configuration())
