# Theme format

Reference for `theme.txt`. See the bundled themes in [themes/](../themes/) for complete examples.

A theme is a directory with a `theme.txt`, images (PNG/JPEG/TGA) and fonts (`.pf2`). The syntax follows GRUB menu themes. Global settings use `name: "value"`, and screen elements use `+ type { name = "value" ... }`. Quotes are optional for values without spaces. `#` starts a comment, and `-` and `_` in names are interchangeable.

## Global settings

| Setting | Default | Meaning |
|---|---|---|
| `background-image` | none | Background picture, relative to the theme directory |
| `background-mode` | `crop` | `crop` (fill, cut edges), `fit` (whole picture, bars), `stretch`, `center` (no scaling) |
| `background-color` | `#000000` | Color behind (or instead of) the image |
| `font-file` | none | Load a `.pf2` font. Repeat for each font/size. |
| `gfxmode` | `keep;auto` | Resolution(s), e.g. `1920x1080,auto` |
| `max-tries` | `3` | Attempts before giving up; `0` = unlimited |

## Elements

Elements are drawn in file order, so later ones are on top.

| Element | Draws |
|---|---|
| `box` | A rectangle (panel, card, underline), optionally with a border |
| `image` | A picture (logo, icon) |
| `label` | A line of text |
| `passphrase` | The input field |

## Element properties

| Property | Applies to | Meaning |
|---|---|---|
| `left`, `top` | all | Position of the top-left corner (see *Coordinates*) |
| `width`, `height` | all | Size. Labels default to their text, images to their natural size (one side given = keep aspect ratio), the field to 300 × font height + padding. |
| `show` | all | `always` (default), or a comma list of `prompt`, `busy`, `error` |
| `color` | box: fill; label, passphrase: text | Box default `#00000080`, text default white |
| `background-color` | label, passphrase | Fill behind the text (default transparent) |
| `border-width`, `border-color` | box, label, passphrase | Border in pixels and its color |
| `padding` | label, passphrase | Inner spacing (field default 8) |
| `font` | label, passphrase | Font name, e.g. `"Noto Sans Regular 20"`. It must match a loaded font exactly. |
| `align` | label, passphrase | `left`, `center`, `right` within the element's width |
| `text` | label | The message (see *Placeholders*) |
| `file` | image | Picture file |
| `mask` | passphrase | Character(s) shown per typed character (default `*`, e.g. `•`) |
| `mask-mode` | passphrase | `per-char` (default), `fixed` (always `fixed-length` marks, hides the length), `hidden` |
| `fixed-length` | passphrase | Number of marks in `fixed` mode (default 8) |
| `placeholder`, `placeholder-color` | passphrase | Hint text shown while empty |
| `cursor-color` | passphrase | Blinking cursor color |
| `id` | all | Ignored by GRUB; for the theme maker |

## Coordinates

| Value | Meaning |
|---|---|
| `120` | 120 pixels |
| `50%` | Half of the screen width (or height) |
| `50%-260` / `100%-48` | Percentage plus or minus pixels |
| `center` | Centered on the screen (uses the element's size) |
| `center+20` | Centered, then shifted 20 pixels |

Use percentages and `center` so a theme works at any resolution.

## Colors

`#RGB`, `#RRGGBB`, or with transparency `#RGBA` / `#RRGGBBAA` (e.g. `#0b1020c8`), or names like `white`.

## States

- `prompt`: waiting for the passphrase
- `busy`: checking the passphrase (key derivation can take several seconds)
- `error`: the last passphrase was wrong; typing continues in this state

## Placeholders in `text`

| Placeholder | Value |
|---|---|
| `{device}` | The UUID or device given to `unlock_theme` |
| `{attempt}` | Current attempt number |
| `{max_tries}` | `max-tries` |
| `{attempts_left}` | Attempts left after the current one |

## Assets and limits

The installer checks all of these before installing.

- **Images:** GRUB decodes the whole picture into memory at 4 bytes per pixel (1080p ≈ 8 MB, 4K ≈ 33 MB). Make the background match the screen resolution to avoid slow scaling.
  - PNG: RGB, RGBA or palette, 8 or 16 bit. Not grayscale, not interlaced.
  - JPEG: baseline only, not progressive.
- **Fonts:** convert with `grub-mkfont -s SIZE -n "Family" -r RANGES -o out.pf2 font.ttf`, or use the `font` helper in `themes/common.sh`.
  - Every size is a separate file, and glyphs are not anti-aliased.
  - GRUB names a font `<family> <Regular|Bold|…> <size>`, and `font = "..."` must match that name.
  - Every character used in text, masks and placeholders must be in a loaded font (`grub-mkfont` silently drops characters the TTF doesn't have).
- **Keyboard:** GRUB uses a US layout and ASCII passphrases, the same limits as the stock prompt.
