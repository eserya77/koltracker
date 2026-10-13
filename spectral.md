He creado en .env SPECTRAL_BOT_TOKEN, este es un bot distinto pero básicamente se nutre del mismo programa.

Básicamente para todos sus usuarios, envía los tokens del kol bot ( llamémos así al programa ya construido ) que están siendo trackeados por el trending por más de 10 minutos ( por defecto ).

Además añade otra regla para los tokens que se mantienen trackeados por trending, actualmente se encuentra en trending todos los tokens que no han caído 70% desde su ATH, ahora añade otra regla que se quitan también si CAEN de 2k mcap.

Además de que se envía tokens vivos por más de 10 minutos, permite que esto sea editable desde un menú settings. Y también permite que sea editable el rango de multiplier que el usuario quiere.

Por ejemplo, si un token se empieza a trackear a 50k mcap, y actualmente está en 110k mcap, y los filtros del usuario son de 0.7x a 2x, entonces el token no se le envía, ya que queda fuera de sus filtros.

Añade un comando /show que envía todos los tokens que cumplen sus filtros.

También se envían todos los eventos para los tokens que caen dentro de sus filtros, por ejemplo si se le envió un Token A, y ese token migra, se envía por SPECTRAL el evento de migración, de liquidez, si otro KOL compra, si hay un dex paid, cto, o boost, etc... Acá sí no hay trending alerts, solo se nutre de él.

