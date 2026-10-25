"""Catalogue de grands groupes qui recrutent beaucoup en France, interrogés par leur API carrières publique.

Chaque entrée a été vérifiée (octobre 2026) : Workday se décrit par son hôte et son site, les autres ATS par
leur identifiant. Les entreprises déjà déclarées dans « companies » ne sont pas interrogées deux fois.
Les groupes dont le site carrières n'a pas d'API ouverte (Capgemini, Orange, Société Générale…) restent
couverts par les agrégateurs (LinkedIn, Indeed, HelloWork, Welcome to the Jungle).
"""
from . import ats

GROUPS = [
    # Workday
    {"name": "Thales", "ats": "workday", "host": "thales.wd3.myworkdayjobs.com", "site": "Careers"},
    {"name": "Airbus", "ats": "workday", "host": "ag.wd3.myworkdayjobs.com", "site": "Airbus"},
    {"name": "Accenture", "ats": "workday", "host": "accenture.wd103.myworkdayjobs.com", "site": "AccentureCareers"},
    {"name": "Sanofi", "ats": "workday", "host": "sanofi.wd3.myworkdayjobs.com", "site": "SanofiCareers"},
    {"name": "Air Liquide", "ats": "workday", "host": "airliquidehr.wd3.myworkdayjobs.com",
     "site": "AirLiquideExternalCareer"},
    {"name": "Valeo", "ats": "workday", "host": "valeo.wd3.myworkdayjobs.com", "site": "valeo_jobs"},
    {"name": "Michelin", "ats": "workday", "host": "michelinhr.wd3.myworkdayjobs.com", "site": "Michelin"},
    {"name": "Stellantis", "ats": "workday", "host": "stellantis.wd3.myworkdayjobs.com",
     "site": "External_Career_Site_ID01"},
    {"name": "Pernod Ricard", "ats": "workday", "host": "pernodricard.wd3.myworkdayjobs.com", "site": "pernod-ricard"},
    {"name": "Hewlett Packard Enterprise", "ats": "workday", "host": "hpe.wd5.myworkdayjobs.com", "site": "Jobsathpe"},
    {"name": "Cisco", "ats": "workday", "host": "cisco.wd5.myworkdayjobs.com", "site": "Cisco_Careers"},
    {"name": "Salesforce", "ats": "workday", "host": "salesforce.wd12.myworkdayjobs.com", "site": "External_Career_Site"},
    # SmartRecruiters
    {"name": "Alten", "ats": "smartrecruiters", "slug": "Alten"},
    {"name": "Assystem", "ats": "smartrecruiters", "slug": "Assystem"},
    {"name": "Artelia", "ats": "smartrecruiters", "slug": "Artelia"},
    {"name": "Forvis Mazars", "ats": "smartrecruiters", "slug": "Mazars"},
    {"name": "Ubisoft", "ats": "smartrecruiters", "slug": "Ubisoft2"},
    {"name": "Accor", "ats": "smartrecruiters", "slug": "Accor"},
    {"name": "Bosch", "ats": "smartrecruiters", "slug": "BoschGroup"},
    {"name": "Sopra Steria", "ats": "smartrecruiters", "slug": "SopraSteria1"},
    {"name": "Devoteam", "ats": "smartrecruiters", "slug": "Devoteam"},
    # Greenhouse
    {"name": "Cloudflare", "ats": "greenhouse", "slug": "cloudflare"},
    {"name": "SentinelOne", "ats": "greenhouse", "slug": "sentinellabs"},
    {"name": "Okta", "ats": "greenhouse", "slug": "okta"},
    {"name": "Elastic", "ats": "greenhouse", "slug": "elastic"},
    {"name": "MongoDB", "ats": "greenhouse", "slug": "mongodb"},
    {"name": "Datadog", "ats": "greenhouse", "slug": "datadog"},
    {"name": "Doctolib", "ats": "greenhouse", "slug": "doctolib"},
    {"name": "Mirakl", "ats": "greenhouse", "slug": "mirakl"},
    {"name": "Algolia", "ats": "greenhouse", "slug": "algolia"},
    {"name": "Dataiku", "ats": "greenhouse", "slug": "dataiku"},
    # Lever
    {"name": "Contentsquare", "ats": "lever", "slug": "contentsquare"},
    {"name": "Aircall", "ats": "lever", "slug": "aircall"},
    {"name": "Pigment", "ats": "lever", "slug": "pigment"},
    {"name": "BlaBlaCar", "ats": "lever", "slug": "blablacar"},
    {"name": "Swile", "ats": "lever", "slug": "swile"},
    {"name": "Malt", "ats": "lever", "slug": "malt"},
    {"name": "Vestiaire Collective", "ats": "lever", "slug": "vestiairecollective"},
]


def _key(company: dict) -> tuple:
    return company.get("ats"), str(company.get("slug") or company.get("host") or "").lower()


def collect(criteria: dict, cfg: dict, known_urls: set, log) -> list[dict]:
    declared = {_key(c) for c in cfg.get("companies", [])} if cfg.get("sources", {}).get("ats") else set()
    companies = [c for c in GROUPS if _key(c) not in declared]
    return ats.collect(criteria, cfg, known_urls, log, companies=companies)
