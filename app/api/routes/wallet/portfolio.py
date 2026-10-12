"""
routers/portfolio.py  –  /api/wallet/portfolio
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from app.schemas.wallet import (
    PortfolioPositionDB,
    PortfolioPositionDBCreate,
    PortfolioPositionDBUpdate,
    PortfolioPositionAddRequest,
    PortfolioPositionSellRequest,
    PortfolioPositionCloseRequest,
)
from app.models.wallet import WalletPosition
from app.models.price import Price
from app.models.stock_indicators import StockIndicator
from app.models.market_reports import NetShortPosition
from app.models.static_stock_info import StaticStockInfo
from app.models.rs_daily import RSDaily
from app.models.user import User
from app.core.database import get_db
from app.api.deps import get_current_user

router = APIRouter()


@router.get(
    "/positions",
    response_model=List[dict],
    summary="Retrieve all active wallet positions with latest market prices and indicators",
)
def list_positions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[dict]:
    positions = (
        db.query(WalletPosition)
        .filter(WalletPosition.user_id == current_user.id)
        .order_by(WalletPosition.created_at.desc())
        .all()
    )

    result = []
    for pos in positions:
        # Latest price record
        latest_price = db.query(Price).filter(
            Price.symbol == pos.symbol
        ).order_by(Price.date.desc()).first()

        # Latest indicators record
        latest_ind = db.query(StockIndicator).filter(
            StockIndicator.symbol == pos.symbol
        ).order_by(StockIndicator.date.desc()).first()

        # Latest short position record
        latest_short = db.query(NetShortPosition).filter(
            NetShortPosition.symbol == pos.symbol
        ).order_by(NetShortPosition.report_date.desc()).first()

        # Static info (including marginable_percent)
        static_info = db.query(StaticStockInfo).filter(
            StaticStockInfo.symbol == pos.symbol
        ).first()

        # Latest RS daily record
        latest_rs = db.query(RSDaily).filter(
            RSDaily.symbol == pos.symbol
        ).order_by(RSDaily.date.desc()).first()

        current_price = float(latest_price.close) if latest_price else float(pos.buy_price)
        prev_close = float(latest_price.open) if latest_price and latest_price.open else current_price
        change_pct = float(latest_price.change_percent) / 100 if latest_price and latest_price.change_percent else 0.0

        # Get marginable_percent from StaticStockInfo (if available), otherwise from prices table
        marginable_percent = None
        if static_info and static_info.marginable_percent:
            marginable_percent = float(static_info.marginable_percent)
        elif latest_price and latest_price.marginable_percent:
            marginable_percent = float(latest_price.marginable_percent)

        pos_dict = {
            "id": pos.id,
            "symbol": pos.symbol,
            "name": pos.name or (latest_price.company_name if latest_price and latest_price.company_name else ""),
            "qty": float(pos.qty),
            "buy_price": float(pos.buy_price),
            "stop_price": float(pos.stop_price) if pos.stop_price else None,
            "current_price": current_price,
            "portfolio_name": pos.portfolio_name,
            "entry_date": pos.entry_date.isoformat() if pos.entry_date else None,
            "created_at": pos.created_at.isoformat() if pos.created_at else None,
            "updated_at": pos.updated_at.isoformat() if pos.updated_at else None,
            # Enriched from prices table
            "sector": latest_price.sector if latest_price else None,
            "industry_group": latest_price.industry_group if latest_price else None,
            "change_percent": change_pct,
            "marginable_percent": marginable_percent,
            "short_percent": float(latest_short.percent_over_free_float) if latest_short and latest_short.percent_over_free_float else None,
            # Enriched from stock_indicators table
            "percent_change_20d": float(latest_ind.percent_change_20d) if latest_ind and latest_ind.percent_change_20d else None,
            "percent_change_63d": float(latest_ind.percent_change_63d) if latest_ind and latest_ind.percent_change_63d else None,
            "percent_change_126d": float(latest_ind.percent_change_126d) if latest_ind and latest_ind.percent_change_126d else None,
            "percent_change_15d": float(latest_ind.percent_change_15d) if latest_ind and latest_ind.percent_change_15d else None,
            "sma_150": float(latest_ind.sma_150) if latest_ind and latest_ind.sma_150 else None,
            "trend_signal": bool(latest_ind.trend_signal) if latest_ind else False,
            "final_signal": bool(latest_ind.final_signal) if latest_ind else False,
            "score": int(latest_ind.score) if latest_ind and latest_ind.score else 0,
            # Enriched from RSDaily
            "rs_rating": int(latest_rs.rs_rating) if latest_rs and latest_rs.rs_rating is not None else None,
            "rank_1m": int(latest_rs.rank_1m) if latest_rs and latest_rs.rank_1m is not None else None,
            "rank_3m": int(latest_rs.rank_3m) if latest_rs and latest_rs.rank_3m is not None else None,
            "rank_6m": int(latest_rs.rank_6m) if latest_rs and latest_rs.rank_6m is not None else None,
            "rank_9m": int(latest_rs.rank_9m) if latest_rs and latest_rs.rank_9m is not None else None,
            "rank_12m": int(latest_rs.rank_12m) if latest_rs and latest_rs.rank_12m is not None else None,
            "transactions": pos.transactions,
        }
        result.append(pos_dict)

    return result


@router.post(
    "/positions",
    response_model=PortfolioPositionDB,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new wallet position",
)
def create_position(
    req: PortfolioPositionDBCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PortfolioPositionDB:
    position = WalletPosition(
        user_id=current_user.id,
        symbol=req.symbol,
        name=req.name,
        qty=req.qty,
        buy_price=req.buy_price,
        stop_price=req.stop_price,
        portfolio_name=req.portfolio_name or "Default",
        entry_date=req.entry_date,
    )
    db.add(position)
    db.commit()
    db.refresh(position)
    return position


@router.get(
    "/positions/{position_id}",
    response_model=PortfolioPositionDB,
    summary="Retrieve a wallet position by ID",
)
def get_position(
    position_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PortfolioPositionDB:
    position = (
        db.query(WalletPosition)
        .filter(WalletPosition.id == position_id, WalletPosition.user_id == current_user.id)
        .first()
    )
    if not position:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")
    return position


@router.put(
    "/positions/{position_id}",
    response_model=PortfolioPositionDB,
    summary="Update an existing wallet position",
)
def update_position(
    position_id: int,
    req: PortfolioPositionDBUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PortfolioPositionDB:
    position = (
        db.query(WalletPosition)
        .filter(WalletPosition.id == position_id, WalletPosition.user_id == current_user.id)
        .first()
    )
    if not position:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")

    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(position, field, value)

    db.commit()
    db.refresh(position)
    return position


@router.delete(
    "/positions/{position_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a wallet position",
)
def delete_position(
    position_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    position = (
        db.query(WalletPosition)
        .filter(WalletPosition.id == position_id, WalletPosition.user_id == current_user.id)
        .first()
    )
    if not position:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")
    db.delete(position)
    db.commit()
    return None


@router.post(
    "/positions/{position_id}/close",
    summary="Close a position and move it to the tracker",
)
def close_position(
    position_id: int,
    req: PortfolioPositionCloseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.wallet import WalletTrade
    from datetime import date

    position = (
        db.query(WalletPosition)
        .filter(WalletPosition.id == position_id, WalletPosition.user_id == current_user.id)
        .first()
    )
    if not position:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")

    current_price = req.sell_price

    qty = float(position.qty)
    buy_price = float(position.buy_price)

    realized_pnl = (current_price - buy_price) * qty
    pnl_pct = realized_pnl / (buy_price * qty) if buy_price * qty > 0 else 0.0

    entry_date = position.entry_date or date.today()
    exit_date = req.exit_date
    days_held = max((exit_date - entry_date).days, 0)

    trade = WalletTrade(
        user_id=current_user.id,
        symbol=position.symbol,
        realized_pnl=realized_pnl,
        pnl_pct=pnl_pct,
        days_held=days_held,
        exit_date=exit_date,
    )

    db.add(trade)
    db.delete(position)
    db.commit()

    return {"message": "Position closed successfully", "realized_pnl": realized_pnl}


@router.post(
    "/positions/{position_id}/add",
    response_model=PortfolioPositionDB,
    summary="Add shares to an existing position (Scaling in)",
)
def add_shares_to_position(
    position_id: int,
    req: PortfolioPositionAddRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    position = (
        db.query(WalletPosition)
        .filter(WalletPosition.id == position_id, WalletPosition.user_id == current_user.id)
        .first()
    )
    if not position:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")

    old_qty = float(position.qty)
    old_price = float(position.buy_price)
    new_qty = req.qty
    new_price = req.buy_price

    total_qty = old_qty + new_qty
    weighted_price = ((old_qty * old_price) + (new_qty * new_price)) / total_qty

    position.qty = total_qty
    position.buy_price = weighted_price

    # record transaction
    txs = list(position.transactions) if position.transactions else []
    txs.append({
        "type": "add",
        "date": req.trade_date.isoformat(),
        "qty": new_qty,
        "price": new_price
    })
    position.transactions = txs

    db.commit()
    db.refresh(position)
    return position


@router.post(
    "/positions/{position_id}/sell",
    summary="Partial sell shares from an existing position",
)
def partial_sell_position(
    position_id: int,
    req: PortfolioPositionSellRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.wallet import WalletTrade
    from datetime import date

    position = (
        db.query(WalletPosition)
        .filter(WalletPosition.id == position_id, WalletPosition.user_id == current_user.id)
        .first()
    )
    if not position:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Position not found")

    old_qty = float(position.qty)
    sell_qty = req.qty

    if sell_qty > old_qty:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot sell more than held quantity")

    buy_price = float(position.buy_price)
    realized_pnl = (req.sell_price - buy_price) * sell_qty
    pnl_pct = realized_pnl / (buy_price * sell_qty) if buy_price * sell_qty > 0 else 0.0

    entry_date = position.entry_date or date.today()
    days_held = max((req.trade_date - entry_date).days, 0)

    # create a wallet trade for the sold part
    trade = WalletTrade(
        user_id=current_user.id,
        symbol=position.symbol,
        realized_pnl=realized_pnl,
        pnl_pct=pnl_pct,
        days_held=days_held,
        exit_date=req.trade_date,
    )
    db.add(trade)

    if sell_qty == old_qty:
        db.delete(position)
        db.commit()
        return {"message": "Position fully closed", "realized_pnl": realized_pnl, "closed": True}
    else:
        position.qty = old_qty - sell_qty
        txs = list(position.transactions) if position.transactions else []
        txs.append({
            "type": "sell",
            "date": req.trade_date.isoformat(),
            "qty": sell_qty,
            "price": req.sell_price,
            "pnl": realized_pnl
        })
        position.transactions = txs
        db.commit()
        db.refresh(position)
        return {"message": "Position partially sold", "realized_pnl": realized_pnl, "closed": False}


from pydantic import BaseModel

class CashUpdate(BaseModel):
    cash: float

@router.get(
    "/settings/cash",
    summary="Get available cash",
)
def get_cash(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.wallet import WalletSetting
    setting = db.query(WalletSetting).filter(
        WalletSetting.user_id == current_user.id,
        WalletSetting.key == "portfolio_cash"
    ).first()
    if not setting:
        return {"cash": 0.0}
    return {"cash": float(setting.value.get("amount", 0.0))}

@router.put(
    "/settings/cash",
    summary="Update available cash",
)
def update_cash(
    req: CashUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.wallet import WalletSetting
    setting = db.query(WalletSetting).filter(
        WalletSetting.user_id == current_user.id,
        WalletSetting.key == "portfolio_cash"
    ).first()
    if not setting:
        setting = WalletSetting(
            user_id=current_user.id,
            key="portfolio_cash",
            value={"amount": req.cash}
        )
        db.add(setting)
    else:
        setting.value = {"amount": req.cash}
    db.commit()
    return {"cash": req.cash}

@router.get(
    "/transactions",
    summary="Get all transactions from all positions",
)
def get_all_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    positions = db.query(WalletPosition).filter(WalletPosition.user_id == current_user.id).all()
    all_txs = []
    
    # 1. Transactions from open positions
    for pos in positions:
        if pos.transactions:
            for tx in pos.transactions:
                all_txs.append({
                    "id": f"pos_{pos.id}_{tx.get('date')}_{tx.get('type')}",
                    "symbol": pos.symbol,
                    "type": tx.get("type", "buy"),
                    "date": tx.get("date"),
                    "qty": float(tx.get("qty", 0)),
                    "price": float(tx.get("price", 0)),
                    "status": "completed",
                    "value": float(tx.get("qty", 0)) * float(tx.get("price", 0))
                })

    # 2. Closed trades (WalletTrades) as sell transactions
    from app.models.wallet import WalletTrade
    trades = db.query(WalletTrade).filter(WalletTrade.user_id == current_user.id).all()
    for tr in trades:
        all_txs.append({
            "id": f"trade_{tr.id}",
            "symbol": tr.symbol,
            "type": "sell",
            "date": tr.exit_date.isoformat() if tr.exit_date else None,
            "qty": 0, # Closed trades do not store quantity in wallet_trades
            "price": 0,
            "status": "completed",
            "value": float(tr.realized_pnl)
        })

    # sort by date descending
    all_txs.sort(key=lambda x: x["date"] or "", reverse=True)
    return all_txs

@router.get(
    "/events",
    summary="Get portfolio events (dividends are still mocked; report events come from XBRL)",
)
def get_portfolio_events(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import datetime
    from app.models.wallet import WalletPosition

    today = datetime.date.today()

    positions = db.query(WalletPosition).filter(WalletPosition.user_id == current_user.id).all()
    symbols = [p.symbol for p in positions]

    # The income_statements table was dropped on 2026-10-06 (0 rows since creation —
    # financial statements are served from the XBRL JSON store, see
    # app/services/xbrl_data_service.py). The contract below is unchanged: an empty
    # "financials" list is exactly what this endpoint returned before the drop.
    financials: list = []
    return {
        "dividends": [
            { "id": "mock_1", "symbol": "2222", "type": "dividend", "title": "توزيع أرباح (تجريبي)", "date": (today + datetime.timedelta(days=12)).isoformat(), "amount": 0.31, "status": "upcoming" }
        ],
        "financials": financials
    }

@router.get(
    "/performance",
    summary="Get historical portfolio performance (On-the-fly calculation with real TWR)",
)
def get_portfolio_performance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import datetime
    from app.models.wallet import WalletPosition, WalletSetting, WalletTrade
    from app.models.price import Price
    
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=30)
    
    positions = db.query(WalletPosition).filter(WalletPosition.user_id == current_user.id).all()
    symbols = [p.symbol for p in positions]
    
    # Get cash setting
    setting = db.query(WalletSetting).filter(
        WalletSetting.user_id == current_user.id,
        WalletSetting.key == "portfolio_cash"
    ).first()
    current_cash = float(setting.value.get("amount", 0.0)) if setting else 0.0
    
    if not symbols:
        # No positions → return flat zero line
        history = []
        for i in range(30, -1, -1):
            d = today - datetime.timedelta(days=i)
            history.append({
                "date": d.strftime("%m-%d"),
                "sr": 0.0,
                "twr": 0.0,
                "tasi": 0.0
            })
        return history
    
    # Fetch prices for the last 30 days + some buffer for lookback
    buffer_start = start_date - datetime.timedelta(days=10)
    prices = db.query(Price).filter(
        Price.date >= buffer_start,
        Price.symbol.in_(symbols + ["TASI", "^TASI"])
    ).all()
    
    price_map: dict = {}
    for p in prices:
        date_str = p.date.isoformat()
        if date_str not in price_map:
            price_map[date_str] = {}
        price_map[date_str][p.symbol] = float(p.close)
    
    # Collect all cash flow events (buy/sell transactions) for TWR sub-period splitting
    all_cash_flows: list = []  # list of (date, amount) where amount > 0 = inflow, < 0 = outflow
    for pos in positions:
        if pos.transactions:
            for tx in pos.transactions:
                tx_date_str = tx.get('date', '')[:10]
                if not tx_date_str:
                    continue
                try:
                    tx_date = datetime.date.fromisoformat(tx_date_str)
                except ValueError:
                    continue
                tx_qty = float(tx.get('qty', 0))
                tx_price = float(tx.get('price', 0))
                if tx.get('type') in ['buy', 'add']:
                    all_cash_flows.append((tx_date, tx_qty * tx_price))
                elif tx.get('type') == 'sell':
                    all_cash_flows.append((tx_date, -(tx_qty * tx_price)))
    
    all_cash_flows.sort(key=lambda x: x[0])
    
    def get_price_for_symbol_on_date(sym: str, d: datetime.date) -> float | None:
        """Find most recent available price for a symbol on or before a date."""
        for j in range(0, 15):
            check = (d - datetime.timedelta(days=j)).isoformat()
            if check in price_map and sym in price_map[check]:
                return price_map[check][sym]
        return None
    
    def get_tasi_on_date(d: datetime.date) -> float:
        for j in range(0, 15):
            check = (d - datetime.timedelta(days=j)).isoformat()
            if check in price_map:
                if "TASI" in price_map[check]:
                    return price_map[check]["TASI"]
                elif "^TASI" in price_map[check]:
                    return price_map[check]["^TASI"]
        return 10000.0  # fallback
    
    def calc_portfolio_value_on_date(d: datetime.date) -> tuple:
        """Returns (invested_value, total_cost) for all positions on a given date."""
        total_value = 0.0
        total_cost = 0.0
        
        for pos in positions:
            qty_on_date = 0.0
            cost_on_date = 0.0
            if pos.transactions:
                for tx in pos.transactions:
                    tx_date_str = tx.get('date', d.isoformat())[:10]
                    try:
                        tx_date = datetime.date.fromisoformat(tx_date_str)
                    except ValueError:
                        continue
                    if tx_date <= d:
                        tx_qty = float(tx.get('qty', 0))
                        tx_price = float(tx.get('price', 0))
                        if tx.get('type') in ['buy', 'add']:
                            qty_on_date += tx_qty
                            cost_on_date += tx_qty * tx_price
                        elif tx.get('type') == 'sell':
                            qty_on_date -= tx_qty
                            cost_on_date -= tx_qty * tx_price
            else:
                if pos.entry_date and pos.entry_date <= d:
                    qty_on_date = float(pos.qty)
                    cost_on_date = float(pos.qty) * float(pos.buy_price)
            
            if qty_on_date > 0:
                sym_price = get_price_for_symbol_on_date(pos.symbol, d)
                if sym_price is None:
                    sym_price = float(pos.buy_price)
                total_value += qty_on_date * sym_price
                total_cost += cost_on_date
        
        return total_value, total_cost
    
    # ── Build daily history ──
    history = []
    base_tasi_val = None
    
    # TWR state: compound factor
    twr_compound = 1.0
    prev_day_portfolio_value = None
    
    for i in range(30, -1, -1):
        d = today - datetime.timedelta(days=i)
        
        invested_value, total_cost = calc_portfolio_value_on_date(d)
        portfolio_value = invested_value + current_cash
        
        # ── SR (Simple Return): (current_invested_value - total_cost) / total_cost ──
        if total_cost > 0:
            sr = ((invested_value - total_cost) / total_cost) * 100
        else:
            sr = 0.0
        
        # ── TWR (Time-Weighted Return) ──
        # For each day, calculate: sub_period_return = (V_end - V_start - CF) / (V_start + CF_in)
        # Then compound: TWR = product(1 + r_i) - 1
        if prev_day_portfolio_value is not None and prev_day_portfolio_value > 0:
            # Sum of cash flows that happened on this day
            cf_today = sum(cf_amt for cf_date, cf_amt in all_cash_flows if cf_date == d)
            
            # Sub-period return: how much the portfolio grew excluding the effect of cash flows
            v_start = prev_day_portfolio_value
            v_end = portfolio_value
            
            # Adjusted start = start + inflows (invested right at the start of day)
            adjusted_start = v_start + max(cf_today, 0)
            
            if adjusted_start > 0:
                sub_return = (v_end - v_start - cf_today) / adjusted_start
                twr_compound *= (1 + sub_return)
            # else: skip this sub-period (division by zero edge case)
        
        prev_day_portfolio_value = portfolio_value
        
        twr = (twr_compound - 1) * 100
        
        # ── TASI ──
        tasi_price = get_tasi_on_date(d)
        if base_tasi_val is None:
            base_tasi_val = tasi_price
        tasi_pct = ((tasi_price - base_tasi_val) / base_tasi_val * 100) if base_tasi_val else 0.0
        
        history.append({
            "date": d.strftime("%m-%d"),
            "sr": round(sr, 2),
            "twr": round(twr, 2),
            "tasi": round(tasi_pct, 2)
        })

    return history


@router.get(
    "/summary",
    summary="Get portfolio summary computed on-the-fly",
)
def get_portfolio_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns aggregated portfolio stats:
    - total_value (stocks + cash)
    - stocks_value
    - total_cost
    - cash
    - unrealized_pnl, unrealized_pnl_pct
    - realized_pnl (from closed trades)
    - num_positions
    """
    from app.models.wallet import WalletPosition, WalletSetting, WalletTrade

    positions = db.query(WalletPosition).filter(WalletPosition.user_id == current_user.id).all()
    
    setting = db.query(WalletSetting).filter(
        WalletSetting.user_id == current_user.id,
        WalletSetting.key == "portfolio_cash"
    ).first()
    cash = float(setting.value.get("amount", 0.0)) if setting else 0.0
    
    total_cost = 0.0
    stocks_value = 0.0
    
    for pos in positions:
        qty = float(pos.qty)
        buy_p = float(pos.buy_price)
        total_cost += qty * buy_p
        
        latest_price = db.query(Price).filter(
            Price.symbol == pos.symbol
        ).order_by(Price.date.desc()).first()
        
        current_price = float(latest_price.close) if latest_price else buy_p
        stocks_value += qty * current_price
    
    unrealized_pnl = stocks_value - total_cost
    unrealized_pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0.0
    
    # Realized P&L from closed trades
    trades = db.query(WalletTrade).filter(WalletTrade.user_id == current_user.id).all()
    realized_pnl = sum(float(t.realized_pnl) for t in trades)
    
    return {
        "total_value": round(stocks_value + cash, 2),
        "stocks_value": round(stocks_value, 2),
        "total_cost": round(total_cost, 2),
        "cash": round(cash, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        "realized_pnl": round(realized_pnl, 2),
        "num_positions": len(positions),
    }


@router.get(
    "/realized-pnl",
    summary="Get closed trade history with realized P&L",
)
def get_realized_pnl(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.models.wallet import WalletTrade
    
    trades = db.query(WalletTrade).filter(
        WalletTrade.user_id == current_user.id
    ).order_by(WalletTrade.exit_date.desc()).all()
    
    result = []
    for t in trades:
        result.append({
            "id": t.id,
            "symbol": t.symbol,
            "realized_pnl": float(t.realized_pnl),
            "pnl_pct": float(t.pnl_pct) * 100,
            "days_held": t.days_held,
            "exit_date": t.exit_date.isoformat() if t.exit_date else None,
        })
    
    return result
