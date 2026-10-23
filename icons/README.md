# Icon packs

The graphics stage drops a still image onto the timeline when a word in the
transcript matches a trigger. Resolve composites the alpha natively, so the result
stays fully editable afterwards -- there is no separate renderer and nothing is
burned into the video.

Icon packs are **not included**. Supply your own art; put each game's pack in
`icons/<game>/` and point the game pack's `icon_map` at its `map.json`.

## Important: bake position into the image

The interchange formats used here cannot reliably express scale or position for an
image clip. So each PNG must be a **full-frame, mostly-transparent canvas** at your
timeline resolution (e.g. 1920x1080), with the icon already drawn where you want it
to appear. `position` in the map below is a convenience for the generator, not
something Resolve reads.

## Do not put digits in PNG filenames

Resolve reads trailing digits in an image filename as an image-sequence frame
number and will refuse the clip or mis-import it. Use `left_stick.png`,
not `left_stick_01.png`.

## `map.json` format

```json
{
  "canvas": {"width": 1920, "height": 1080},
  "defaults": {
    "size_px": 240,
    "margin_px": 60,
    "hold_seconds": 2.5,
    "cooldown_seconds": 10.0
  },
  "icons": {
    "left_back": {
      "image": "left_stick.png",
      "triggers": ["left back", "left full back"],
      "position": "top-left"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `canvas` | Must match your timeline resolution. |
| `defaults.hold_seconds` | How long an icon stays once triggered. |
| `defaults.cooldown_seconds` | Minimum gap before the same icon can fire again. |
| `icons.<key>.triggers` | Phrases to match in the transcript. List **every** spelling and both languages if the speaker code-switches. |
| `icons.<key>.position` | `top-left`, `top-right`, `bottom-left`, `bottom-right`. |

## Known limitation

Hold and cooldown are fixed durations. They do not follow the speaker's phrasing or
tempo, so a fast sequence of triggers can look mechanical. Timing that tracks the
phrase is an open design problem -- see the status section in the README.
