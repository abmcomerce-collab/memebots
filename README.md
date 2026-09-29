# Liga de 100 bots

10 subligas (memecoins, cripto grandes, Nasdaq, sectores USA, IBEX 35, índices desarrollados, emergentes, divisas, materias primas y bonos) con 10 bots cada una, 10 € virtuales por bot y precios reales de cierre diario (CoinGecko y Yahoo Finance, con Stooq de reserva).

## Bots de cada subliga
- 1 benchmark: compra todo el mercado a partes iguales y aguanta.
- 1 aleatorio: grupo de control que cambia de cartera al azar.
- 1 IA (Claude): opera con las órdenes de ia_orders.json.
- 7 estrategias documentadas: momentum transversal (Jegadeesh y Titman), momentum de serie temporal (Moskowitz, Ooi y Pedersen), filtro de media de 200 (Faber), cruce de medias, ruptura de Donchian, RSI(2) de Connors, bandas de Bollinger y momentum dual (Antonacci).

Solo largos, sin apalancamiento. Coste por operación según el mercado (ver config.json).

## Reglas de la liga
- Un bot muere si pierde la mitad del capital (salvo benchmark y aleatorio, que son controles).
- Cada 14 días se cierra ronda. En cada subliga se eliminan como máximo 2 bots: los muertos y los que, con al menos 28 días de vida, quedan por debajo del aleatorio y del benchmark. Entran estrategias nuevas con otros parámetros.
- El ranking general usa el exceso de rentabilidad sobre el benchmark de la propia subliga, junto con Sharpe y caída máxima.

## Ejecución
GitHub Actions ejecuta league.py cada día a las 22:30 UTC y publica data.json (web) y LEADERBOARD.md. Prueba local: python league.py --mock 120
