"""Matinee — play your own video library on a Tronbyt.

The heavy lifting happens elsewhere: a Matinee daemon on your own machine
decodes your video files, scales them to 64x32 and slices them into
animated-WebP chunks. This app is the thin end of that pipe — it asks the
daemon for whichever chunk is current and hands it to the display.

The daemon decides what "current" means, so playback position, skipping and
episode rollover are all controlled there rather than here. Every fetch
inside one chunk's window returns the same bytes, which keeps this app
correct even though the server may re-render it more often than the device
actually shows it.
"""

load("http.star", "http")
load("render.star", "render")
load("schema.star", "schema")

DEFAULT_URL = "http://tronbyt-host.local:8765/chunk"

# The daemon serves one chunk per request and never blocks, so there is
# nothing to gain from caching — and a cached chunk would freeze playback.
NO_CACHE = 0

def main(config):
    url = config.str("url")
    if not url:
        # Render something sane before the app is configured: the linter and
        # the app-store preview both run main() with no config, and a fetch
        # here would fail the app outright.
        return _notice("Set Matinee URL")

    resp = http.get(url, ttl_seconds = NO_CACHE)

    if resp.status_code == 503:
        # Daemon is up but has nothing to play yet.
        return _notice("Nothing ingested")
    if resp.status_code != 200:
        return _notice("Matinee %d" % resp.status_code)

    chunk = render.Image(src = resp.body())

    return render.Root(
        # The chunk is already 64x32 at the right frame rate, so there is
        # nothing to lay out — just show it.
        child = chunk,
        # Pixlet applies ONE delay to the whole animation rather than honouring
        # per-frame timing, so this has to be the chunk's real frame interval
        # or playback runs fast. The daemon guarantees every frame in a chunk
        # has the same duration precisely so this single value is correct.
        delay = chunk.delay,
        # Chunks are several seconds long; without this the device would cut
        # away partway through one.
        show_full_animation = True,
    )

def _notice(text):
    return render.Root(
        child = render.Box(
            child = render.WrappedText(
                content = text,
                font = "tom-thumb",
                align = "center",
            ),
        ),
    )

def get_schema():
    return schema.Schema(
        version = "1",
        fields = [
            schema.Text(
                id = "url",
                name = "Matinee URL",
                desc = "The chunk endpoint of your Matinee daemon, " +
                       "e.g. http://your-host:8765/chunk",
                icon = "film",
                default = DEFAULT_URL,
            ),
        ],
    )
