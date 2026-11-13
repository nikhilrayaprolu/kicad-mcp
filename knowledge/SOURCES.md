# Knowledge sources

`rv1106.yaml` cites every fact as `src: <file>#p<page>`. The cited documents are third-party and copyrighted, so they are
**not redistributed** here. Download them into `knowledge/sources/` to re-check or re-extract:

| File | Publisher | Where |
|---|---|---|
| `Rockchip_RV1106_Datasheet_V2.2-20260602.pdf` | Rockchip | LuckfoxTECH/Luckfox-Pico-docs repository on GitHub |
| `RV1106_Datasheet_V1.7.pdf` | Rockchip | files.luckfox.com |
| `Rockchip_RV1106_User_Manual_GPIO.pdf` | Rockchip | LuckfoxTECH/Luckfox-Pico-docs repository on GitHub |
| `Luckfox-Pico-Pro-Max.pdf` | Luckfox | wiki.luckfox.com / files.luckfox.com (Pico Pro/Max schematic) |
| `Luckfox-Pico-Ultra-W.pdf`, `Luckfox-Pico-Pi.pdf` | Luckfox | wiki.luckfox.com / files.luckfox.com |

`scripts/` holds the extraction helpers. `gen_pins_yaml.py` regenerates the pin list from the datasheet text dump.
To add a new part, follow the same pattern: download its sources, extract the text with pymupdf, and write `knowledge/<part>.yaml` with
the package, pins (number → datasheet name → type), power rails, required external parts and interface pins, each with `src:` cites.
