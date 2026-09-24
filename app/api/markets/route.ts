import { NextResponse } from "next/server";

const BUDA_API_BASE = "https://www.buda.com/api/v2";

const CLP_MARKETS = [
  "BTC-CLP",
  "ETH-CLP",
  "BCH-CLP",
  "LTC-CLP",
  "USDC-CLP",
  "USDT-CLP",
  "SOL-CLP",
] as const;

type BudaAmount = [string, string];

interface BudaTicker {
  market_id: string;
  last_price: BudaAmount;
  min_ask: BudaAmount;
  max_bid: BudaAmount;
  volume: BudaAmount;
  quote_volume: BudaAmount;
  price_variation_24h: string;
  price_variation_7d: string;
}

export interface MarketSnapshot {
  marketId: string;
  baseCurrency: string;
  quoteCurrency: string;
  lastPrice: number;
  minAsk: number;
  maxBid: number;
  volume: number;
  priceVariation24h: number;
  priceVariation7d: number;
}

async function fetchTicker(marketId: string): Promise<MarketSnapshot> {
  const res = await fetch(`${BUDA_API_BASE}/markets/${marketId}/ticker`, {
    next: { revalidate: 10 },
  });

  if (!res.ok) {
    throw new Error(`Buda ticker request failed for ${marketId}: ${res.status}`);
  }

  const { ticker } = (await res.json()) as { ticker: BudaTicker };
  const [base, quote] = ticker.market_id.split("-");

  return {
    marketId: ticker.market_id,
    baseCurrency: base,
    quoteCurrency: quote,
    lastPrice: Number(ticker.last_price[0]),
    minAsk: Number(ticker.min_ask[0]),
    maxBid: Number(ticker.max_bid[0]),
    volume: Number(ticker.volume[0]),
    priceVariation24h: Number(ticker.price_variation_24h),
    priceVariation7d: Number(ticker.price_variation_7d),
  };
}

export async function GET() {
  try {
    const snapshots = await Promise.all(CLP_MARKETS.map(fetchTicker));
    return NextResponse.json({ markets: snapshots });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
