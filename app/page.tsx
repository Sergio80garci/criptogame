import type { MarketSnapshot } from "./api/markets/route";

async function getMarkets(): Promise<MarketSnapshot[]> {
  const res = await fetch(
    `${process.env.NEXT_PUBLIC_BASE_URL ?? "http://localhost:3000"}/api/markets`,
    { cache: "no-store" }
  );
  const data = await res.json();
  return data.markets ?? [];
}

export default async function Home() {
  const markets = await getMarkets();

  return (
    <main style={{ padding: 24, fontFamily: "monospace" }}>
      <h1>Crypto Quest — verificación de datos Buda.com (CLP)</h1>
      <table cellPadding={8} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th align="left">Mercado</th>
            <th align="right">Último precio</th>
            <th align="right">Var. 24h</th>
            <th align="right">Var. 7d</th>
            <th align="right">Volumen 24h</th>
          </tr>
        </thead>
        <tbody>
          {markets.map((m) => (
            <tr key={m.marketId} style={{ borderTop: "1px solid #ccc" }}>
              <td>{m.marketId}</td>
              <td align="right">{m.lastPrice.toLocaleString("es-CL")}</td>
              <td align="right">{(m.priceVariation24h * 100).toFixed(2)}%</td>
              <td align="right">{(m.priceVariation7d * 100).toFixed(2)}%</td>
              <td align="right">
                {m.volume.toFixed(4)} {m.baseCurrency}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
