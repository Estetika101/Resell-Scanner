# sgw-watch terms: rationale

26 watches, 74 keywords (the existing 13 camera watches add 38; total ~112, under the ~150 budget). Built 2026-11-03.

Sources, in the order they count:
- **2024 sheet**: `Flipping 2.0.xlsx`, 186 sales Jan 2024 – Jan 2025, as recorded in `flip/archive/scanner/config.yaml` and `README.md`. These are realized sale prices or nets.
- **Marginspace**: the sold lane, read 2026-11-03 (68 rows, 113 copies). The read tools show landed cost and the last asking price. They do not show the realized sale price. So wherever a note cites Marginspace, the price it gives is "listed at", not "sold for".
- **Outside**: PriceCharting (fetched 2026-11-03; the pages show no date), TechRepublic (2026-05-04) and Mental Floss (2024-11-21). Outside sources only ever justify `new` or `try`.

Tiers: 15 `winner`, 6 `new`, 5 `try`.

## Cameras (11)
- **Winners from the 2024 sheet's measured models.** The seed does not already cover these:
  - Canon A-series: family n=15, $20 median net.
  - Coolpix S220/S550/L10/L11/3100/3200.
  - Cyber-shot W5/W330/W560.
  - Cyber-shot P7 and its siblings (family median $42.50; P200 excluded because it sold for $8).
  - Olympus FE-220/FE-4020/VG-140.
  - Value is per family, not per SKU (README), which is why siblings ride on the family's numbers.
- **Every compact stays at or under $35.** Most sit at $20–30, because the losses came from misvaluing.
- **ELPH SD siblings are `new`.** The family sold in Marginspace (SD200 and SD770), but those exact SKUs did not.
- **Mavica is `new`.** The 2024 sheet records three sales, but no net for them.
- **G10/G11/G12 are `try` at $75.** The G12 is the best camera sale ever ($276 net), but it cost $0, so it proves resale value and says nothing about what to pay. The G9 cost $184.79 landed and lost about $50. $75 sits under the ~$90 prosumer cap.
- **Kodak V-series, GoPro Hero4/5 and Polaroid are `try`.** Kodak compacts are thin. GoPro has one sale. Polaroid is Peter's 2026-08-17 exemption from the film-camera rule, and he has no sales of it yet.

## Game consoles (3)
- **NES ($80):** two best 2024 games sales. About 12 sold in Marginspace, landed $37.60–$150.30.
- **GameCube ($70):** two sold in Marginspace, landed $70.45 and $111.09.
- **PS1/PS2 ($35):** went 3-for-3 in 2024 for $67 net. PS3 and later are excluded.
- Clones, HDMI units and cartridge-only listings are excluded.

## Handhelds (5)
- **GBA SP AGS-101, SP AGS-001, original GBA and PSP are `new`.** The evidence is PriceCharting loose prices only. The two SP models are separate watches, because the 101 is worth about 1.6x as much.
- **DS Lite is `try` at $25.** The 2024 DS data is n=2, and the scanner's ceiling on it was inflated (SPEC 4.6).

## NES games (2)
- **Zelda (gold) and Punch-Out singles are `winners`.** Three of each sold in Marginspace, at $8.75–$23.09 landed.
- The costs were allocated from lots, and game lots lost money in 2024. Both watches therefore exclude "lot", and cheap titles are left out: Platoon, Baseball and Rollergames listed below their floors.

## iPods (2)
- **Classic/Video ($40) and Nano ($20) are `winners`.** iPods went 5-for-5 in 2024 ($99 net), and TechRepublic (2026-05-04) reports rising searches.
- Caveat: the sheet names only one model (a Nano A1236), so the Classic tier rests on the family result.

## Electronics (3)
- **Numark TTX:** netted $106 and $102.
- **Epson Home Cinema 3500:** $158 net, one sale.
- **CRT TVs:** $60 net each, but on $0 cost. Max $20, which only works as local pickup.

## Left out on purpose
- **Banned by the rules:** film cameras, bridge/superzoom bodies (SX two-digit, H/HX, SP-UZ, Kodak Z, Coolpix L120/P), Casio Exilim, Olympus Stylus (despite one Stylus 410 in Marginspace), Flip/PlaySport, power tools, peripherals, multi-unit audio, computers, untested camcorder lots.
- **No usable evidence:** N64, SNES, Genesis, Game Gear and Game Boy Color have no sales of his and no price I fetched. The Genesis Terapeak comps are market data only.
- **Sources too weak:** MiniDisc (blog-grade sourcing only) and camcorders (also against the rule).
- **Too thin a margin:** the Meta Quest 2 sold at a $125 ask on $90.44 landed.
- **Single sales:** the Jamstik, the Game Genie and other one-off NES titles.
- **Never sold:** designer clothing, which also carries counterfeit risk.

## Doubts
- **Keywords may miss listings.** `config.yaml` says ShopGoodwill ORs multi-word terms. If it does, a "Brand Model" keyword may never surface the model inside the 80 soonest-ending results. Yet "sony cybershot" returning 24 suggests AND. The seed uses the same keyword shape, so watch the first scans for zero-hit keywords.
- **No import path.** sgw-watch has export but no import route. Loading this file needs either a small importer or entry by hand.

## Search words (added after review, 2026-11-02)

ShopGoodwill ORs the words of a search (measured by the retired scanner:
flip `archive/scanner/config.yaml`, live term audit), so a keyword like
"CRT TV" or "NES console" returns everything with "TV" or "console" in it and
crowds real listings out of the pages roger reads. Each watch now has an
optional `search` list: the single distinctive words sent to ShopGoodwill
(Mavica, NES, GameCube, PSP, iPod, CRT…). The `keywords` still decide what
counts as a match. Watches without a `search` list search each keyword's model
number on its own (SX110), or the keyword as written.

After the first few scans, check **Scan log → Keywords**: zero matched with
many results is noise; zero results is either nothing listed or a bad word.
