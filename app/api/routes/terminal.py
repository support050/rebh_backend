from fastapi import APIRouter, Depends
from app.services import terminal_service
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/api/terminal", tags=["Terminal Aggregations"])


@router.get("/market-machine")
@router.get("/market-machine/")
def get_market_machine(current_user: User = Depends(get_current_user)):
    return terminal_service.get_market_machine_data()


@router.get("/quant-lab")
@router.get("/quant-lab/")
def get_quant_lab(current_user: User = Depends(get_current_user)):
    return terminal_service.get_quant_lab_data()


@router.get("/all-ratios")
@router.get("/all-ratios/")
def get_all_ratios(current_user: User = Depends(get_current_user)):
    return terminal_service.get_all_ratios_data()


@router.get("/audit-summary")
@router.get("/audit-summary/")
def get_audit_summary(current_user: User = Depends(get_current_user)):
    return terminal_service.get_audit_summary_data()


@router.get("/company-fundamental/{symbol}")
@router.get("/company-fundamental/{symbol}/")
def get_company_fundamental(symbol: str, current_user: User = Depends(get_current_user)):
    return terminal_service.get_company_unified_page_data(symbol)


@router.get("/sector-templates")
@router.get("/sector-templates/")
def get_sector_templates(current_user: User = Depends(get_current_user)):
    return terminal_service.get_sector_templates_master_data()


