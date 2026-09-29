# Liga de memebots

Cinco bots con 10 € virtuales cada uno operan memecoins con precios reales de CoinGecko.
Cada hora GitHub Actions ejecuta `bots.py` y actualiza `LEADERBOARD.md`.

## Reglas
- Comisión simulada: 1 % por operación (0,5 % de Fomo + deslizamiento).
- Si un bot baja de 5 €, muere al instante (vende todo y deja de operar).
- Cada 14 días se cierra la ronda: salen los muertos y los peores hasta sumar 2 eliminados, y entran bots nuevos con estrategias aleatorias y 10 €.
- La IA (Claude) también puede ser eliminada. Si muere, está fuera.

## Montaje (10 minutos)
1. Crea un repositorio **público** en GitHub (en públicos, Actions es gratis y sin límite) y sube estos archivos, incluida la carpeta `.github`.
2. Opcional: crea una clave demo gratis en CoinGecko y guárdala en Settings → Secrets → Actions como `COINGECKO_API_KEY`.
3. Pestaña Actions → "Liga memebots" → Run workflow. Si sale en verde, ya corre solo cada hora.
4. Trading floor visual: Settings → Pages → Source "Deploy from a branch" → rama `main`, carpeta `/ (root)` → Save. En un par de minutos tendrás la web en `https://TU-USUARIO.github.io/NOMBRE-REPO/`. Se refresca sola cada 5 minutos.

## Bot IA
Cuando quieras que Claude opere, pásale el enlace a tu `LEADERBOARD.md`. Te devolverá una orden para añadir a `ia_orders.json`, por ejemplo:
`{"id": "2026-10-01a", "action": "buy", "token": "BONK", "pct": 100, "nota": "motivo"}`
Se ejecuta en la siguiente hora.

## Probar en local
`python bots.py --mock 800` simula 800 horas con precios inventados. Borra `state.json` después.
