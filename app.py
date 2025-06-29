import asyncio
import aiohttp
import time
import re
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

app = FastAPI()

MIN_PROFIT = 5
MIN_SPREAD = 5
MAX_BUY_PRICE = 1000
PLATFORM = "pc"
MIN_QUANTITY = 1
CONCURRENT_REQUESTS = 100
TIMEOUT = 5

# --- Paste your existing helper functions below (fetch_json, fetch_items, get_order_rank, group_orders_by_rank, process_item) ---

async def fetch_json(session, url, sem):
    async with sem:
        try:
            async with session.get(url, timeout=TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    return None
                else:
                    print(f"HTTP {resp.status} on {url}")
        except Exception as e:
            print(f"Exception fetching {url}: {e}")
            return None
    return None

async def fetch_items(session, sem):
    url = "https://api.warframe.market/v1/items"
    data = await fetch_json(session, url, sem)
    if data:
        return [item["url_name"] for item in data["payload"]["items"]]
    return []

def get_order_rank(order):
    for key in ("mod_rank", "rank", "level"):
        rank = order.get(key)
        if isinstance(rank, int):
            return rank

    item_name = ""
    if "item" in order and order["item"]:
        item_name = order["item"].get("url_name", "") or order["item"].get("item_name", "")
    if not item_name:
        item_name = order.get("item_name", "")

    match = re.search(r"rank[_ ]?(\d+)", item_name.lower())
    if match:
        return int(match.group(1))

    status = order.get("user", {}).get("status", "")
    match = re.search(r"rank[_ ]?(\d+)", status.lower())
    if match:
        return int(match.group(1))

    return None

def group_orders_by_rank(orders, order_type):
    filtered = [
        o for o in orders
        if o["order_type"] == order_type
        and o["visible"]
        and o["user"]["platform"] == PLATFORM
        and o["quantity"] >= MIN_QUANTITY
        and o["platinum"] > 0
        and o["user"].get("status") in ("online", "ingame")
    ]
    rank_groups = {}
    for o in filtered:
        rank = get_order_rank(o)
        rank_groups.setdefault(rank, []).append(o)
    return rank_groups

async def process_item(session, item_url, budget, sem):
    url = f"https://api.warframe.market/v1/items/{item_url}/orders"
    data = await fetch_json(session, url, sem)
    if not data:
        return None
    orders = data.get("payload", {}).get("orders", [])
    if not orders:
        return None

    buy_groups = group_orders_by_rank(orders, "buy")
    sell_groups = group_orders_by_rank(orders, "sell")

    best_trade = None

    for rank, buys in buy_groups.items():
        sells = sell_groups.get(rank)
        if not sells:
            continue

        best_buy = max(buys, key=lambda o: o["platinum"])
        best_sell = min(sells, key=lambda o: o["platinum"])

        buy_price = best_sell["platinum"]
        sell_price = best_buy["platinum"]

        if buy_price < MAX_BUY_PRICE and sell_price > buy_price:
            spread = sell_price - buy_price
            if spread >= MIN_SPREAD:
                profit_per_unit = spread
                max_units = min(best_buy["quantity"], best_sell["quantity"])
                max_affordable_units = budget // buy_price
                units_to_trade = min(max_units, max_affordable_units, 1)  # limit to 1 unit

                if units_to_trade > 0:
                    total_profit = profit_per_unit * units_to_trade
                    if total_profit >= MIN_PROFIT:
                        trade = {
                            "item": item_url,
                            "buy_price": buy_price,
                            "sell_price": sell_price,
                            "spread": spread,
                            "units": units_to_trade,
                            "profit_per_unit": profit_per_unit,
                            "total_profit": total_profit,
                            "rank": rank,
                        }
                        if not best_trade or trade["total_profit"] > best_trade["total_profit"]:
                            best_trade = trade

    return best_trade

# --- Web endpoint replacing your CLI main() ---
@app.get("/trades")
async def get_trades(
    budget: int = Query(..., gt=0, description="How much platinum you want to invest"),
    trades_to_show: int = Query(7, gt=0, le=20, description="How many trades to show")
):
    sem = asyncio.Semaphore(CONCURRENT_REQUESTS)
    start_time = time.time()

    async with aiohttp.ClientSession() as session:
        items = await fetch_items(session, sem)
        if not items:
            return JSONResponse(status_code=500, content={"error": "Failed to fetch item list"})

        tasks = [process_item(session, item, budget, sem) for item in items]

        results = []
        done = 0
        total_items = len(items)

        for future in asyncio.as_completed(tasks):
            result = await future
            results.append(result)
            done += 1

        profitable_trades = [r for r in results if r]
        profitable_trades.sort(key=lambda x: x["total_profit"], reverse=True)

        top_trades = profitable_trades[:trades_to_show]

        elapsed = time.time() - start_time

        return {
            "elapsed_time": f"{elapsed:.2f}s",
            "total_items_scanned": total_items,
            "trades_returned": len(top_trades),
            "trades": top_trades,
        }
