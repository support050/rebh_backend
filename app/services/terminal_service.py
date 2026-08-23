"""
Terminal Service Entrypoint & Backwards Compatibility Layer.
Proxies to modular services in `app.services.terminal.*`.
"""
from app.services.terminal.market_machine_service import get_market_machine_data
from app.services.terminal.quant_lab_service import get_quant_lab_data, get_all_ratios_data
from app.services.terminal.forensic_service import get_audit_summary_data, get_company_unified_page_data
from app.services.terminal.sector_templates_service import get_sector_templates_master_data

__all__ = [
    "get_market_machine_data",
    "get_quant_lab_data",
    "get_all_ratios_data",
    "get_audit_summary_data",
    "get_company_unified_page_data",
    "get_sector_templates_master_data",
]
