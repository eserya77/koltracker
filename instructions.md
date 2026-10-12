Read this:

https://pumpapi.io/llms-full.txt

Also read kolsides.json

Con el datastream de pumpapi necesito trackear wallets, las que están en kolsides.json, las que son el número 1, quiere decir que es la main wallet, por ejemplo Cented1, es la main wallet de Cented. Las que no tienen el número 1 son "Side wallets"

Todas las side wallets cuentan como 1 sola.

El objetivo es crear un bot de telegram en python que haga lo siguiente:

Habrá un grupo de telegram con topics, el id del grupo es: -1003922183481

Trending topic: -1003922183481_6
Main wallets topic: -1003922183481_2
Side wallets topic: -1003922183481_4
Events topic: -1003922183481_8

Trending topic: Trackea el marketcap de los tokens haciendo uso de pumpapi datastream, todos los tokens comprados por 2 totalmente distintos KOLs tanto en sus Side wallets, como en sus main wallets son trackeados, y se reporta con un mensaje de trending cada vez que aumenta el multiplier, por ejemplo: 1.3x, 1.6x, 2x, 2.5x, 3x, 4x, 5x, 6x, 7x, 8x, 9x, 10x, 13x, 16x, 19x, 20x, 25x, 30x, 40x, 50x y luego de diez en diez. Presta atención a la fórmula de la secuencia. Además debe mostrar en cuánto tiempo pasó ese multiplier. El token deja de ser observado si cae 70% de su punto más alto.

Main wallets topic: Todas las compras de la main wallet van aquí. Si tienen comprado en sides, listar también todas sus side wallets, cuánto compraron, si aún holdean todo lo que compraron, parcialmente, o ya vendieron por completo, y marcar su avg entry market cap.

Side wallets topic: Todas las compras de sidewallets, poner data importante, cuántas veces han comprado este token, cuánto sol, cuánto tiempo llevan holdeando, etc...

Events topic: Eventos de migración, dex pagado, cto, etc, lee la api de dexscreener tienen un websocket para todos estos eventos.

Crea una base de datos coherente para soportar este sistema.

Pumpapi tiene un policy de 1 stream por IP, y yo ya tengo uno conectada aquí C:\Users\ADM\Desktop\automated_signals así que deberás usar esa.

Si puedes enriquecer la data con la que se hace pues genial, por ejemplo añadir de que venue compraron, puede ser Orca, Pumpswap, pumpfun, raydium, o todas las que pumpapi soporta.

Añadir cada vez que un KOL provee liquidity ( en events ), pumpapi tiene para eso.