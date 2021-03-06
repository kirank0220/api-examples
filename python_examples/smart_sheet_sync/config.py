#########################################################################
#    _________        ___.                   ______________________  ___
#    \_   ___ \___.__.\_ |__   ___________  /  _____/\______   \   \/  /
#    /    \  \<   |  | | __ \_/ __ \_  __ \/   \  ___ |       _/\     /
#    \     \___\___  | | \_\ \  ___/|  | \/\    \_\  \|    |   \/     \
#     \______  / ____| |___  /\___  >__|    \______  /|____|_  /___/\  \
#            \/\/          \/     \/               \/        \/      \_/
#
#

import os
import re
import json
import requests
import smartsheet
import stringcase
from openpyxl import Workbook, load_workbook
from tqdm import tqdm
from utils import (
    split,
    category_match,
    normalize_vendor,
    lookup_sheet_id,
    skip_falsy,
    insert_http,
    validate_answer,
    date_or_none,
    email_metadata,
    required,
    valid_assessment_order,
    as_string,
    inherent_risk_level_from_tier,
)
from glom import glom, Coalesce, OMIT

import click

HEADER_MAPPING = {
    "Vendor Name": "company_name",
    "Vendor URL": "company_url",
    # Address metadata (need at least a city to create a company)
    "Vendor HQ City": "address_city",
    "Vendor HQ Country": "address_country",
    # Internal Metadata
    "Vendor Owner": "internal_vendor_owner",
    "Description": "internal_description",
    "Location": "internal_location",
    # Third Party Contact Info
    "Vendor Contact Name": "third_party_contact_name",
    "Vendor Contact First Name": "third_party_contact_first_name",
    "Vendor Contact Last Name": "third_party_contact_last_name",
    "Vendor Contact Email": "third_party_contact_email",
    "Vendor Contact Phone": "third_party_contact_phone",
    # Assessment Order Info
    "Order Assessment Tier": "assessment_order",
    # Cyber classification metadata
    "Critical/Support": "meta_is_critical_or_support",
    "RTO": "meta_rto",
    "Data Sensitivity": "meta_data_sensitivity",
    "Legal/Regulatory Compliance": "meta_compliance",
    "Technology Risk": "meta_tech_risk",
    "Influence": "meta_influence",
    # Profile questions
    "Digital Identities": "profile_digital_identities",
    "People": "profile_people",
    "Data": "profile_data",
    "Applications": "profile_applications",
    "Devices": "profile_devices",
    "Network Access": "profile_networks",
    "Facilities": "profile_facilities",
    "Business Process": "profile_business_process",
    # GRX Metadata tracking from smart-sheets
    "Ingest Date": "ingest_date",
}

SMART_SHEET_UPDATE_COLUMNS = {
    "GRX Vendor Name": {"key": "grx_vendor_name", "spec": "grx.name"},
    "Impact": {"key": "impact", "spec": "grx.impact"},
    "Likelihood": {"key": "likelihood", "spec": "grx.likelihood"},
    "Industry": {"key": "industry", "spec": "grx.industry"},
    "CyberGRX Inherent Risk Level": {"key": "grx_inherent_risk_level", "spec": "grx.inherent_risk_level"},
    "Is GRX Profile Complete": {"key": "is_profile_complete", "spec": "grx.is_profile_complete"},
    "Is GRX Report Available": {"key": "is_report_available", "spec": "grx.is_report_available"},
    "GRX Subscription Status": {"key": "grx_subsctiption_status", "spec": "grx.subsctiption_status"},
    "GRX Assessment Status": {"key": "grx_assessment_status", "spec": "grx.assessment_status"},
    "GRX Assessment Progress": {"key": "grx_assessment_progress", "spec": "grx.assessment_progress"},
    "GRX Assessment Completion Date": {
        "key": "grx_assessment_completion_date",
        "spec": "grx.assessment_completion_date",
    },
    "GRX Assessment Requested Date": {
        "key": "grx_assessment_requested_completion_date",
        "spec": "grx.assessment_requested_completion_date",
    },
}

SMART_SHEET_COLUMNS_FOR_RESIDUAL_RISK_OUTCOMES = {
    "Inherent Data Loss Score": {
        "key": "grx_inherent_data_loss_score",
        "spec": "grx.residual_risk_data_loss_inherent_risk_level",
    },
    "Residual Data Loss Score": {
        "key": "grx_residual_data_loss_score",
        "spec": "grx.residual_risk_data_loss_residual_risk_level",
    },
    "Inherent Disruptive Attack Score": {
        "key": "grx_inherent_disruptive_attack_score",
        "spec": "grx.residual_risk_disruptive_attack_inherent_risk_level",
    },
    "Residual Disruptive Attack Score": {
        "key": "grx_residual_disruptive_attack_score",
        "spec": "grx.residual_risk_disruptive_attack_residual_risk_level",
    },
    "Inherent Destructive Attack Score": {
        "key": "grx_inherent_destructive_attack_score",
        "spec": "grx.residual_risk_destructive_attack_inherent_risk_level",
    },
    "Residual Destructive Attack Score": {
        "key": "grx_residual_destructive_attack_score",
        "spec": "grx.residual_risk_destructive_attack_residual_risk_level",
    },
    "Inherent Fraud Score": {
        "key": "grx_inherent_fraud_score",
        "spec": "grx.residual_risk_fraud_inherent_risk_level",
    },
    "Residual Fraud Score": {
        "key": "grx_residual_fraud_score",
        "spec": "grx.residual_risk_fraud_residual_risk_level",
    },
}

# Ingect columns for residual risk outcomes
SMART_SHEET_UPDATE_COLUMNS.update(SMART_SHEET_COLUMNS_FOR_RESIDUAL_RISK_OUTCOMES)

# Ingect column keys into header mapping
for k, v in SMART_SHEET_UPDATE_COLUMNS.items():
    HEADER_MAPPING[k] = v["key"]

COMPANY_SCHEMA = {
    "name": "company_name",
    # Prefer the company domain from the spread sheet, fallback to using the email address's domain
    "url": Coalesce(
        (
            Coalesce(("company_url", required), ("third_party_contact_email", email_metadata("domain"))),
            required,
            insert_http,
        ),
        default=OMIT,
    ),
    "custom_id": ("custom_id", as_string),
    "ingest_date": (Coalesce("ingest_date", default=None), date_or_none),
    "address": (
        {
            "city": Coalesce(("address_city", skip_falsy), default=OMIT),
            "country": Coalesce(("address_country", skip_falsy), default=OMIT),
        },
        skip_falsy,
    ),
    # Map the assessment order column to a valid order request, skip this field if it is not present or invalid
    "order_info": Coalesce(("assessment_order", valid_assessment_order), default=OMIT),
    "third_party_contact": (
        {
            # Prefer the first and last name from the spread sheet, fallback to using the email address
            "first_name": (
                Coalesce(
                    ("third_party_contact_first_name", required),
                    ("third_party_contact_name", split(False), required),
                    ("third_party_contact_email", email_metadata("first_name")),
                    default=OMIT,
                ),
                skip_falsy,
            ),
            "last_name": (
                Coalesce(
                    ("third_party_contact_last_name", required),
                    ("third_party_contact_name", split(True), required),
                    ("third_party_contact_email", email_metadata("last_name")),
                    default=OMIT,
                ),
                skip_falsy,
            ),
            "email": Coalesce(("third_party_contact_email", skip_falsy), default=OMIT),
            "phone": Coalesce(("third_party_contact_phone", as_string, skip_falsy), default=OMIT),
        },
        skip_falsy,
    ),
    # If the row has answers, configure the scoping profile payload
    "third_party_scoping": (
        {
            "digital_identities": Coalesce(("profile_digital_identities", validate_answer), default=OMIT),
            "people": Coalesce(("profile_people", validate_answer), default=OMIT),
            "data": Coalesce(("profile_data", validate_answer), default=OMIT),
            "applications": Coalesce(("profile_applications", validate_answer), default=OMIT),
            "devices": Coalesce(("profile_devices", validate_answer), default=OMIT),
            "networks": Coalesce(("profile_networks", validate_answer), default=OMIT),
            "facilities": Coalesce(("profile_facilities", validate_answer), default=OMIT),
            "business_process": Coalesce(("profile_business_process", validate_answer), default=OMIT),
        },
        skip_falsy,
    ),
    "custom_metadata": (
        {
            "internal": (
                {
                    "owner": Coalesce(("internal_vendor_owner", skip_falsy), default=OMIT),
                    "description": Coalesce(("internal_description", skip_falsy), default=OMIT),
                    "location": Coalesce(("internal_description", skip_falsy), default=OMIT),
                },
                skip_falsy,
            ),
            "cyber_classification": (
                {
                    "critical_or_support": Coalesce(("meta_is_critical_or_support", skip_falsy), default=OMIT),
                    "rto": Coalesce(("meta_rto", skip_falsy), default=OMIT),
                    "data_sensitivity": Coalesce(("meta_data_sensitivity", skip_falsy), default=OMIT),
                    "compliance": Coalesce(("meta_compliance", skip_falsy), default=OMIT),
                    "tech_risk": Coalesce(("meta_tech_risk", skip_falsy), default=OMIT),
                    "influence": Coalesce(("meta_influence", skip_falsy), default=OMIT),
                },
                skip_falsy,
            ),
        },
        skip_falsy,
    ),
}

GRX_COMPANY_SCHEMA = {
    "id": "id",
    "name": "name",
    "custom_id": "custom_id",
    "subsctiption_status": Coalesce("subscription.status", default=None),
    "is_profile_complete": Coalesce("subscription.is_profile_complete", default=False),
    "is_report_available": Coalesce("subscription.is_report_available", default=False),
    "impact": Coalesce("inherent_risk.impact_label", default="Unknown"),
    "likelihood": Coalesce("inherent_risk.likelihood_label", default="Unknown"),
    "inherent_risk_level": (
        Coalesce("inherent_risk.recommended_report_tier", default=0),
        inherent_risk_level_from_tier,
    ),
    "industry": Coalesce("industry", default="Unknown"),
    "assessment_status": Coalesce("assessment.status", default=None),
    "assessment_progress": Coalesce("assessment.progress", default=None),
    "assessment_completion_date": Coalesce("assessment.completion_date", default=None),
    "assessment_requested_completion_date": Coalesce("assessment.requested_completion_date", default=None),
}

RESIDUAL_RISK_PILLS_SCHEMA = {
    "residual_risk_data_loss_inherent_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Data Loss"),
            "inherent_risk_level",
        ),
        default=None,
    ),
    "residual_risk_data_loss_residual_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Data Loss"),
            "residual_risk_level",
        ),
        default=None,
    ),
    "residual_risk_disruptive_attack_inherent_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Disruptive Attack"),
            "inherent_risk_level",
        ),
        default=None,
    ),
    "residual_risk_disruptive_attack_residual_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Disruptive Attack"),
            "residual_risk_level",
        ),
        default=None,
    ),
    "residual_risk_destructive_attack_inherent_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Destructive Attack"),
            "inherent_risk_level",
        ),
        default=None,
    ),
    "residual_risk_destructive_attack_residual_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Destructive Attack"),
            "residual_risk_level",
        ),
        default=None,
    ),
    "residual_risk_fraud_inherent_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Fraud"),
            "inherent_risk_level",
        ),
        default=None,
    ),
    "residual_risk_fraud_residual_risk_level": Coalesce(
        (
            Coalesce("residual_risk.residual_risk_outcomes", default=[]),
            category_match("Fraud"),
            "residual_risk_level",
        ),
        default=None,
    ),
}

GRX_COMPANY_SCHEMA.update(RESIDUAL_RISK_PILLS_SCHEMA)

BULK_IMPORT_COLUMNS = [
    ["Third Party Legal or DBA Name", "company_name", "blue"],
    ["Website Name URL", "company_url", "blue"],
    ["Third Party HQ City", "address_city"],
    ["Third Party HQ Country", "address_country"],
]
