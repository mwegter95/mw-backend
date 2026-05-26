"""
Yard tracker seed data — the 16 plants and 30 tasks originally hard-coded in
the yard.jsx Claude Artifact. Each new growyard user is seeded with this so
they have a working starter database; zweetztuph@gmail.com is seeded once via
the CLI below.

Usage:
    python -m yard_seed --email zweetztuph@gmail.com
"""
import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

# Paths — resolved relative to this file so they work in both dev and prod.
DEFAULTS_DIR = Path(__file__).parent / "yard_photos" / "defaults"
PHOTOS_DIR   = Path(__file__).parent / "data" / "yard_photos"


def _copy_default_photos(owner_id: str) -> None:
    """Copy default plant photos into this owner's photo folder.
    Skips files that already exist so it's safe to call repeatedly."""
    dest = PHOTOS_DIR / str(owner_id)
    dest.mkdir(parents=True, exist_ok=True)
    if not DEFAULTS_DIR.exists():
        return
    for src in DEFAULTS_DIR.iterdir():
        target = dest / src.name
        if not target.exists():
            shutil.copy2(str(src), str(target))


PLANTS = [
    {
        "id": "lilac",
        "common": "Common Lilac",
        "latin": "Syringa vulgaris",
        "type": "shrub",
        "location": "Driveway side / garage",
        "sun": "Full sun",
        "water": "Established — only during severe drought (deep soak every 2–3 weeks if no rain for 3+ weeks)",
        "bloomTime": "Old wood — blooms in May on last year's growth",
        "notes": "The big purple-flowered shrub by the garage door. Already done blooming when photographed (spent panicles visible).",
        "careLevel": "Easy",
        "tags": ["spring-bloomer", "old-wood"],
    },
    {
        "id": "vanhouttei-spirea",
        "common": "Vanhoutte Spirea",
        "latin": "Spiraea × vanhouttei",
        "type": "shrub",
        "location": "Front yard, near variegated dogwood",
        "sun": "Full sun to part shade",
        "water": "Drought tolerant once established. 1\" per week first year, then only in severe drought.",
        "bloomTime": "Old wood — cascading white flowers in late May/early June",
        "notes": "The fountain-shaped shrub covered in tiny white flowers. Sometimes called bridal wreath. Blooms on previous year's wood, so timing of pruning is critical.",
        "careLevel": "Easy",
        "tags": ["spring-bloomer", "old-wood"],
    },
    {
        "id": "nannyberry",
        "common": "Nannyberry Viburnum",
        "latin": "Viburnum lentago",
        "type": "large shrub / small tree",
        "location": "Near deck / shed area",
        "sun": "Full sun to part shade",
        "water": "Prefers moist soil but tolerates drought once established. Deep water during dry spells.",
        "bloomTime": "Old wood — flat white flower clusters late May/early June",
        "notes": "Minnesota native. The large flowering shrub with flat-topped white flower clusters. Berries ripen blue-black in fall and feed birds.",
        "careLevel": "Easy",
        "tags": ["spring-bloomer", "old-wood", "native"],
    },
    {
        "id": "variegated-dogwood",
        "common": "Variegated Red-Twig Dogwood",
        "latin": "Cornus alba 'Elegantissima' (Siberian dogwood)",
        "type": "shrub",
        "location": "Front yard, paver bed",
        "sun": "Full sun to part shade",
        "water": "Prefers moist soil. 1\" per week, especially during dry summer stretches.",
        "bloomTime": "Inconspicuous white flowers in June",
        "notes": "The cream-and-green variegated shrub in the front bed. Grown for foliage and red winter stems. Stems are most vivid red on NEW growth, so periodic hard pruning keeps the winter color show going.",
        "careLevel": "Easy",
        "tags": ["foliage", "winter-interest"],
    },
    {
        "id": "coralberry",
        "common": "Coralberry",
        "latin": "Symphoricarpos orbiculatus",
        "type": "shrub",
        "location": "Hillside area",
        "sun": "Sun to part shade",
        "water": "Very drought tolerant. Rarely needs supplemental water once established.",
        "bloomTime": "Small pink flowers in summer; coral-pink berries in fall/winter",
        "notes": "Spreads by suckers — can form colonies on slopes (good for erosion control). The dense, low shrub mass.",
        "careLevel": "Easy",
        "tags": ["native-ish", "erosion-control"],
    },
    {
        "id": "honeysuckle-invasive",
        "common": "Morrow's Honeysuckle ⚠️",
        "latin": "Lonicera morrowii",
        "type": "invasive shrub",
        "location": "Various spots in yard",
        "sun": "Sun to part shade",
        "water": "N/A — recommended for removal",
        "bloomTime": "Pale yellow paired flowers in May; red berries late summer",
        "notes": "This is a RESTRICTED NOXIOUS WEED in Minnesota. The MN Department of Agriculture strongly encourages removal. Sale and propagation are prohibited. Birds spread the seeds into native woodlands where it crowds out everything else. The berries are also bad nutrition for birds — empty calories that displace native fruits. See the dedicated task to plan removal.",
        "careLevel": "Remove",
        "tags": ["invasive", "remove"],
    },
    {
        "id": "hostas",
        "common": "Hostas (mixed varieties)",
        "latin": "Hosta spp.",
        "type": "perennial",
        "location": "Shaded beds, retaining wall, hillside",
        "sun": "Part shade to full shade",
        "water": "1\" per week; deep watering preferred over frequent shallow. Wilting in afternoon heat is normal — recovers overnight.",
        "bloomTime": "Lavender or white flower spikes in mid-late summer",
        "notes": "You've got several types: solid green large-leaf, blue-green, white-edged variegated, and some smaller varieties. Slugs are the main pest. Voles can chew crowns in winter.",
        "careLevel": "Easy",
        "tags": ["shade", "perennial"],
    },
    {
        "id": "ostrich-fern",
        "common": "Ostrich Fern",
        "latin": "Matteuccia struthiopteris",
        "type": "perennial fern",
        "location": "Shaded hillside, near hummingbird feeder area",
        "sun": "Part shade to full shade",
        "water": "Loves consistent moisture. Will go dormant early if it dries out.",
        "bloomTime": "Non-flowering. Sends up edible fiddleheads in early May.",
        "notes": "MN native. Spreads by underground rhizomes — can form colonies. The tall plumey ferns. Fiddleheads are edible if harvested properly in spring (only take half from any one crown).",
        "careLevel": "Easy",
        "tags": ["native", "shade", "fern"],
    },
    {
        "id": "creeping-phlox",
        "common": "Creeping Phlox / Moss Phlox",
        "latin": "Phlox subulata",
        "type": "groundcover perennial",
        "location": "Spilling over the rock retaining wall",
        "sun": "Full sun to part sun",
        "water": "Drought tolerant once established. Water only in extended dry spells.",
        "bloomTime": "Lavender-blue carpet of flowers in May",
        "notes": "The lavender-blue mat cascading over the stone wall. Looks dead by late summer but it's just dormant. Light shearing after bloom keeps it tidy.",
        "careLevel": "Easy",
        "tags": ["groundcover", "spring-bloomer"],
    },
    {
        "id": "chives",
        "common": "Chives",
        "latin": "Allium schoenoprasum",
        "type": "perennial herb",
        "location": "Rock wall planting pocket",
        "sun": "Full sun",
        "water": "Drought tolerant. Water during long dry spells only.",
        "bloomTime": "Lavender-pink puffball flowers in May–June",
        "notes": "Edible! Leaves for cooking, flowers for garnish/cocktails (mild onion). Lavender flowers also make a beautiful pale-pink vinegar.",
        "careLevel": "Easy",
        "tags": ["edible", "herb"],
    },
    {
        "id": "begonias",
        "common": "Wax/Tuberous Begonias",
        "latin": "Begonia spp.",
        "type": "annual",
        "location": "Rock pocket along stairs",
        "sun": "Part sun / shade",
        "water": "Keep evenly moist; don't let dry out completely. Water at base, not on leaves.",
        "bloomTime": "Continuous from planting through frost",
        "notes": "Annuals in MN. Replant each May after frost danger passes (around Mother's Day / May 15). The mixed pink, cream, and red ones you planted in the rock pocket.",
        "careLevel": "Easy",
        "tags": ["annual"],
    },
    {
        "id": "tulips",
        "common": "Tulips",
        "latin": "Tulipa spp.",
        "type": "bulb",
        "location": "Front bed by retaining wall",
        "sun": "Full sun",
        "water": "Don't water dormant bulbs in summer; rely on rain only.",
        "bloomTime": "Late April–early May",
        "notes": "The purple/pink tulips in your front bed. After bloom, let foliage yellow naturally — that's how bulbs recharge for next year. Most tulips decline after 2–3 years; you may want to refresh with new bulbs in fall.",
        "careLevel": "Easy",
        "tags": ["bulb", "spring"],
    },
    {
        "id": "juniper",
        "common": "Spreading Juniper",
        "latin": "Juniperus horizontalis or similar",
        "type": "evergreen shrub",
        "location": "Front yard hillside",
        "sun": "Full sun",
        "water": "Very drought tolerant once established. Almost never needs water.",
        "bloomTime": "Non-flowering evergreen",
        "notes": "The low spreading evergreen on the front hillside. Some browning on top is normal late-summer stress — investigate if it spreads.",
        "careLevel": "Easy",
        "tags": ["evergreen"],
    },
    {
        "id": "lawn",
        "common": "Lawn (turf grass)",
        "latin": "Kentucky bluegrass + fescue mix (likely)",
        "type": "turfgrass",
        "location": "Whole yard",
        "sun": "Mixed",
        "water": "1 to 1.5 inches per week including rain. Water deeply 2–3x/week rather than daily shallow.",
        "bloomTime": "N/A",
        "notes": "Cool-season grass. The bare/sparse strip along the brick foundation drainage rocks needs attention — see the dedicated overseeding task.",
        "careLevel": "Medium",
        "tags": ["lawn"],
    },
    {
        "id": "mulch-beds",
        "common": "Mulch Beds",
        "latin": "—",
        "type": "garden infrastructure",
        "location": "All planting beds",
        "sun": "—",
        "water": "—",
        "bloomTime": "—",
        "notes": "Wood mulch breaks down and needs refreshing roughly every 2 years. The crusty grayish-tan patches visible in your beds are likely slime mold (Fuligo septica, 'dog vomit fungus') — harmless but unsightly. See dedicated task.",
        "careLevel": "Easy",
        "tags": ["infrastructure"],
    },
    {
        "id": "rock-drainage",
        "common": "Foundation Rock Drainage",
        "latin": "—",
        "type": "drainage infrastructure",
        "location": "Along brick wall foundation",
        "sun": "—",
        "water": "—",
        "bloomTime": "—",
        "notes": "The rock channel + downspout extension along your foundation is doing exactly what it should — moving roof water away from the basement wall. Keep it clear of leaves and debris so water can flow.",
        "careLevel": "Easy",
        "tags": ["infrastructure"],
    },
]


# Photo filenames are derived by convention from each plant's id so the
# frontend can build `${BASE_URL}plants/${plant.image}` URLs against the
# files committed under growyard/public/plants/. Patched onto each PLANTS
# dict at module-load time so every code path (initial seed, v2 backfill,
# and the dev CLI) sees the same shape.
for _p in PLANTS:
    _p["image"] = f"{_p['id']}.jpg"
    _p["thumb"] = f"{_p['id']}-thumb.jpg"


TASKS = [
    {
        "id": "mar-inspect",
        "month": 3,
        "plantId": None,
        "category": "general",
        "title": "Walk the yard and assess winter damage",
        "what": "Do a slow walk through every bed. Look for broken branches, vole tunnels in lawn, frost-heaved perennials, and rabbit damage on shrub bark.",
        "why": "Catching damage early gives you time to plan replacements and prune cleanly before sap flow starts. Voles especially can do a lot of hidden damage under snow.",
        "how": "Bring a notebook or your phone. Photograph anything concerning. Look at the base of young shrubs and trees for gnawed bark (rabbits work from snow level down). Push gently on perennial crowns — if they wiggle freely, they've frost-heaved and need to be pressed back into the soil.",
        "when": "Late March when snow is mostly gone. Before buds swell on shrubs.",
        "duration": "1 hour",
    },
    {
        "id": "mar-prune-dogwood",
        "month": 3,
        "plantId": "variegated-dogwood",
        "category": "prune",
        "title": "Hard-prune variegated dogwood for vivid red winter stems",
        "what": "Cut about 1/3 of the oldest, thickest stems all the way to the ground. Leave the younger, thinner stems.",
        "why": "Red-twig dogwoods produce the most intensely colored stems on NEW growth. Old stems fade to dull gray-brown. Removing them forces the plant to push fresh red shoots from the base.",
        "how": "Use bypass loppers. Identify the thickest, oldest canes — they'll be grayish and corky at the base. Cut them flush with the ground (or as close as you can reach). Don't cut everything — leave 2/3 of the plant intact. Every 3–4 years you can do a 'hard rejuvenation' and cut the whole thing to 6 inches if it gets unruly.",
        "when": "Late March or early April, before buds break.",
        "duration": "20 min",
    },
    {
        "id": "apr-rake-beds",
        "month": 4,
        "plantId": None,
        "category": "general",
        "title": "Clear winter debris from beds",
        "what": "Rake out leaves, broken branches, and matted plant material from all planting beds. Cut back any perennial stems left standing.",
        "why": "Decaying material harbors slug eggs and fungal spores. Removing it lets soil warm faster and gives emerging perennials room to come up. Hostas especially must be cleared — their decaying foliage attracts slugs and rot.",
        "how": "Use a small flexible leaf rake (the metal tine kind tears up emerging perennials). Cut any leftover ornamental grass or perennial stems to 2–3 inches above the ground. Toss most of this in yard waste; compost only if you have a hot pile.",
        "when": "Early to mid April once soil isn't soggy.",
        "duration": "1–2 hours for your yard",
    },
    {
        "id": "apr-divide-hostas",
        "month": 4,
        "plantId": "hostas",
        "category": "prune",
        "title": "Divide any overgrown hostas (optional)",
        "what": "Dig up any hosta that's gotten too big or has a dead center. Split it into 2–4 chunks with a sharp spade.",
        "why": "Hostas spread outward and eventually the center dies out. Dividing rejuvenates them AND gives you free plants. Spring is the best time because the cooler weather + spring rain helps them re-establish before summer heat.",
        "how": "Wait until the 'noses' (rolled-up new leaves) are about 2 inches tall — you can see what you're working with but they haven't fully leafed out yet. Dig the whole clump. Use a sharp spade (or a pruning saw for big ones) to slice it into wedges, each with several noses. Replant immediately at the same depth, water deeply.",
        "when": "Mid–late April when noses emerge but before full leaf-out.",
        "duration": "15 min per plant",
    },
    {
        "id": "apr-mulch-refresh",
        "month": 4,
        "plantId": "mulch-beds",
        "category": "general",
        "title": "Refresh mulch (skip beds that still have 2–3 inches)",
        "what": "Top up mulch to a 2–3 inch depth in beds where it's gotten thin. Don't pile it against trunks or stems.",
        "why": "Mulch suppresses weeds, retains moisture, moderates soil temperature, and slowly feeds the soil as it breaks down. Too thick (4+ inches) suffocates roots and encourages slime mold; too thin lets weeds through.",
        "how": "Use shredded hardwood mulch (or your existing type). DON'T pile against tree trunks — leave a 2-3 inch gap (mulch volcanoes rot bark). DON'T add fresh mulch on top of thick old mulch — rake the old stuff out first or pull it back to refresh underneath.",
        "when": "Mid-April to early May, after spring cleanup.",
        "duration": "Depends on # beds; budget a half day",
    },
    {
        "id": "apr-lawn-rake",
        "month": 4,
        "plantId": "lawn",
        "category": "lawn",
        "title": "Light rake the lawn",
        "what": "Use a leaf rake to pull up matted grass and dead thatch. Pick up sticks and any compacted leaves.",
        "why": "Snow mold and matted areas need air. A light raking exposes the crowns of grass plants to sun and lets new growth come through.",
        "how": "Wait until the lawn isn't soggy underfoot (walking on saturated lawn compacts it). Use a flexible leaf rake, not a power dethatcher — that's overkill for spring and damages crowns. Just lift the matted spots.",
        "when": "Mid-April once lawn has dried out from snowmelt.",
        "duration": "1 hour",
    },
    {
        "id": "may-plant-annuals",
        "month": 5,
        "plantId": "begonias",
        "category": "plant",
        "title": "Plant begonias (and other annuals) after last frost",
        "what": "Plant the rock-pocket begonias and any other annuals you want for the season.",
        "why": "Begonias are tropical — they die at 32°F. Twin Cities average last frost is around May 12–15, but late frosts happen. Wait until danger has passed.",
        "how": "Soak the pots in water for 10 minutes before planting. Dig holes 1.5x the rootball width. Mix a handful of compost into each hole. Plant at the same depth they're at in the pot, water in deeply. For tuberous begonias, pinch off the first round of flowers — sounds painful but it makes the plant bushier and bloom harder the rest of the season.",
        "when": "Around Mother's Day (May 10–15). Watch the 10-day forecast for any late frost.",
        "duration": "30 min",
    },
    {
        "id": "may-prune-lilac",
        "month": 5,
        "plantId": "lilac",
        "category": "prune",
        "title": "Prune lilac IMMEDIATELY after it finishes blooming",
        "what": "Cut off spent flower clusters (deadhead). Remove 1–3 of the oldest, thickest trunks at ground level if the shrub is getting too big or woody.",
        "why": "Lilacs bloom on OLD WOOD — they set next year's flower buds in summer on the wood that grew this season. If you prune after July, you cut off next year's flowers. The window is short: as soon as flowers fade, before July.",
        "how": "Snip just below the spent flower cluster (deadheading). For the bigger renewal pruning, identify the thickest, oldest trunks — they'll be the most corky and gnarled. Cut them at ground level. Every 3–4 years, plan a full rejuvenation: remove 1/3 of all old wood. This keeps the shrub vigorous and flowering close to eye level rather than only at the top.",
        "when": "Late May or early June — as soon as the flowers turn brown. Has a hard deadline of July 1.",
        "duration": "30 min",
    },
    {
        "id": "may-prune-spirea",
        "month": 5,
        "plantId": "vanhouttei-spirea",
        "category": "prune",
        "title": "Prune Vanhoutte spirea IMMEDIATELY after blooming",
        "what": "Shape the shrub and remove some of the oldest canes to the ground.",
        "why": "Same rule as lilac — Vanhoutte spirea blooms on old wood. It sets next spring's flower buds during this summer. Pruning later than June 30 will sacrifice next year's flowers.",
        "how": "Two approaches: (1) Light shaping — shear back the tips that bloomed to maintain the fountain shape. (2) Renewal pruning — cut 1/3 of the oldest thickest canes all the way to ground level. The plant will respond with vigorous new growth that will flower beautifully next spring. Both can be done in the same session.",
        "when": "Right after flowers fade (late May / early June). Hard deadline of June 30.",
        "duration": "30 min",
    },
    {
        "id": "may-prune-nannyberry",
        "month": 5,
        "plantId": "nannyberry",
        "category": "prune",
        "title": "Light prune nannyberry if needed (after blooming)",
        "what": "Remove dead, damaged, or crossing branches. Light shaping only — nannyberry doesn't need heavy annual pruning.",
        "why": "Same old-wood rule as lilac and spirea. Pruning after July sacrifices flowers and berries for the next year.",
        "how": "Walk around the shrub with bypass loppers. Look for branches that rub against each other (cut one), branches that died over winter (cut to live tissue), and any really wild leaders that throw off the shape. Don't get aggressive — this plant looks best with a naturalistic shape.",
        "when": "After flowering (late May/early June).",
        "duration": "20 min",
    },
    {
        "id": "may-overseed-bare",
        "month": 5,
        "plantId": "lawn",
        "category": "lawn",
        "title": "Spot-overseed bare strip by foundation (spring window — second-best timing)",
        "what": "Seed the patchy bare strip along the brick foundation/rock drainage area.",
        "why": "Best time to overseed in MN is mid-Aug to mid-Sept. Spring is second-best — you may get spotty results because crabgrass and weeds germinate at the same time, but waiting all the way to August leaves dirt exposed all summer. Doing both spring AND fall gives you the best shot.",
        "how": "Loosen the top inch of soil with a steel rake. Sprinkle a thin layer of compost or topsoil. Spread a sun/shade mix appropriate to that strip's exposure (looks like part shade given the wall). Press seed into soil (foot-tamp or roller — seed needs soil contact, not burial). Cover lightly with straw to retain moisture. WATER LIGHTLY 2x/day until germination (~10–14 days), then transition to less frequent deep watering.",
        "when": "Early–mid May when soil temps hit ~55°F.",
        "duration": "1 hour",
    },
    {
        "id": "jun-deadhead-tulips",
        "month": 6,
        "plantId": "tulips",
        "category": "general",
        "title": "Deadhead tulip flowers; LEAVE the foliage",
        "what": "Snap off the seed pod where the flower was, but leave all the leaves to yellow naturally.",
        "why": "Snapping off the spent flower prevents the bulb from spending energy on seed production. But the LEAVES are the bulb's solar panels — they're recharging next year's flower. Cutting leaves green is the #1 reason tulips fail to rebloom.",
        "how": "Pinch or snip just below the seed pod, leaving the stem and all leaves. Wait until leaves yellow completely (4–6 weeks) before cutting them down. If the yellowing foliage bothers you visually, interplant with hostas or daylilies that hide it.",
        "when": "Mid–late May through June. Don't cut foliage until it's fully yellow.",
        "duration": "10 min",
    },
    {
        "id": "jun-honeysuckle-plan",
        "month": 6,
        "plantId": "honeysuckle-invasive",
        "category": "remove",
        "title": "Plan and begin Morrow's honeysuckle removal",
        "what": "Make a removal plan for the Morrow's honeysuckle on your property. This is a Minnesota Restricted Noxious Weed.",
        "why": "The Minnesota Department of Agriculture lists this as a Restricted Noxious Weed and strongly encourages removal. It crowds out native plants in woodlands (birds eat the berries and spread seeds widely). The berries are also poor nutrition — empty calories that displace native fruits birds actually need before migration. Removing it from your yard cuts off a seed source.",
        "how": "OPTIONS, easiest to hardest: (1) Small plants (under 3 ft) — pull by hand after rain when soil is moist. Get the whole root. (2) Medium plants — use a Weed Wrench tool (rentable from some Twin Cities tool libraries) — leverages the whole plant out. (3) Large established plants — cut to a stump, then immediately paint the cut stump with concentrated glyphosate (Roundup) or triclopyr. Paint within minutes of cutting; the plant pulls the herbicide into its roots and dies. Without herbicide, cut stumps WILL resprout vigorously. Best timing: late summer/early fall when plant is moving sugars down to roots — herbicide goes with it. Also good before fruit ripens to prevent more seed dispersal.",
        "when": "Decision/planning in June. Execution best in late August through September.",
        "duration": "Plan: 30 min. Removal: depends on size, hours to days.",
    },
    {
        "id": "jun-slime-mold",
        "month": 6,
        "plantId": "mulch-beds",
        "category": "general",
        "title": "Address slime mold in mulch (if/when it appears)",
        "what": "The crusty grayish-tan patches in your mulch beds are likely slime mold — most commonly Fuligo septica, 'dog vomit fungus.' Despite the name, it's a harmless protist, not a fungus, and it doesn't hurt plants.",
        "why": "It looks gross but it's actually breaking down decaying mulch and improving your soil. It doesn't attack living plants. However, you can manage it if it bothers you visually, and dialing back conditions that favor it will prevent it from coming back.",
        "how": "TO REMOVE: Scoop it up while it's still soft and yellow/white, BEFORE it crusts and releases spores. Bag it (don't compost unless your pile gets above 140°F). Spores spread by wind and water — high-pressure rinsing actually spreads it. Vinegar spray works on small patches. TO PREVENT: (1) Don't over-mulch — keep it at 2–3 inches max. (2) Fluff/rake mulch periodically to improve airflow. (3) Water in the morning so beds dry by evening. (4) Don't water mulched areas more than necessary — the slime mold loves consistent moisture. Crusted patches that already released spores: just rake them out, they're done.",
        "when": "Watch for it after warm humid rainy stretches, May through September.",
        "duration": "10 min when it appears",
    },
    {
        "id": "jun-water-establish",
        "month": 6,
        "plantId": None,
        "category": "water",
        "title": "Set up deep-watering routine for any new plantings",
        "what": "Any plants installed this year need consistent deep watering through their first summer.",
        "why": "Established perennials and shrubs have deep root systems. First-year plants don't — they'll die in their first dry spell if you don't water them. After year one, most can fend for themselves.",
        "how": "Rule of thumb: 1 inch of water per week including rainfall (use a tuna can or rain gauge to measure). Deep and infrequent (2x/week, 30+ min with a soaker hose) is FAR better than daily shallow watering — encourages roots to grow down. For shrubs: drip the hose at the base for 10–15 min, slow trickle. For perennials: water at the base, not on leaves (wet leaves = disease).",
        "when": "Set up the habit in June; continue weekly through September.",
        "duration": "Ongoing",
    },
    {
        "id": "jul-deep-water",
        "month": 7,
        "plantId": None,
        "category": "water",
        "title": "Deep water during dry stretches (the 1-inch rule)",
        "what": "Hottest, driest month in MN. Your lawn and beds may need supplemental watering.",
        "why": "Once you've had 2+ weeks without an inch of rain, established lawns and shrubs start to stress. Water deeply at this point rather than letting them go fully dormant and trying to revive them later.",
        "how": "LAWN: 1 inch per week total (rain + irrigation). Water 2x/week, 30–45 min per zone on a typical impact sprinkler. Early morning is ideal (4–9 AM) — minimizes evaporation and lets leaves dry by night. Check with a tuna can: when it has 1 inch of water in it, you're done. SHRUBS/PERENNIALS: For established plants, deep soak only during 3+ week dry spells. Place hose at base on slow trickle for 20–30 min. Don't water if leaves wilt only in the afternoon heat — that's normal; check again at 8am, if still wilted then water.",
        "when": "Whenever rainfall is less than 1 inch in any given week.",
        "duration": "Ongoing",
    },
    {
        "id": "jul-no-prune-shrubs",
        "month": 7,
        "plantId": None,
        "category": "prune",
        "title": "DO NOT PRUNE old-wood shrubs now",
        "what": "Hands off the lilac, Vanhoutte spirea, and nannyberry. The window for pruning these closed at the end of June.",
        "why": "These shrubs are setting flower buds RIGHT NOW for next spring. Any cuts you make now will remove next year's flowers. The buds aren't visible yet, but they're forming inside the wood.",
        "how": "If you absolutely must remove a broken branch for safety, do it surgically — just that one cut. Otherwise wait until next May/June.",
        "when": "All of July, August, September, October, and through winter — no pruning of these.",
        "duration": "0 min (this is a 'don't')",
    },
    {
        "id": "jul-mow-high",
        "month": 7,
        "plantId": "lawn",
        "category": "lawn",
        "title": "Set mower deck HIGH (3–4 inches)",
        "what": "Raise the mowing height to its highest setting during July/August heat.",
        "why": "Taller grass shades its own roots and the soil, conserves moisture, and shades out crabgrass seeds before they germinate. Short scalped lawns brown out in August.",
        "how": "Mower deck to 3.5–4 inches. Never cut more than 1/3 of grass blade length in one mowing. Mulch clippings back into the lawn — they return nitrogen.",
        "when": "All of July and August.",
        "duration": "Mowing routine",
    },
    {
        "id": "aug-honeysuckle-execute",
        "month": 8,
        "plantId": "honeysuckle-invasive",
        "category": "remove",
        "title": "Execute honeysuckle removal (prime window opens)",
        "what": "Late August is the start of the best window for cut-stump herbicide treatment.",
        "why": "Late summer/early fall, plants move sugars from leaves down into roots to store for winter. If you cut the stump and immediately paint herbicide on it, the plant carries the herbicide DOWN with the sugars — kills the entire root system. Spring/early summer cuts mostly just resprout because the plant is pushing energy UP.",
        "how": "Cut stems clean and close to the ground with loppers or a saw. WITHIN 5 MINUTES of cutting, paint the freshly cut surface with concentrated herbicide (glyphosate 41% or triclopyr work; look for products labeled for cut-stump treatment). Use a disposable foam brush. Cover the entire cut surface including the outer ring (cambium) — that's the live tissue. The cut should be a clean, level surface; angled cuts let rain wash the herbicide off. Wear gloves and eye protection. Plan to follow up — some stumps will resprout from below the cut even with treatment; cut and re-treat any resprouts you see next year.",
        "when": "Mid-August through October. Avoid days with rain in next 24 hours.",
        "duration": "Varies by plant size",
    },
    {
        "id": "aug-overseed-prep",
        "month": 8,
        "plantId": "lawn",
        "category": "lawn",
        "title": "Prep the bare strip for fall overseeding",
        "what": "Get the bare lawn strip along the foundation ready for the prime overseeding window.",
        "why": "Mid-Aug through mid-Sept is the absolute best window to overseed in MN. Soil is still warm (fast germination) but air is cooling. Weeds are mostly done germinating for the year, so new grass gets less competition. Roots establish before winter and you get a thick lawn in spring.",
        "how": "About a week before seeding: rake out any dead grass and weeds. Loosen the top 1/2 inch of soil with a steel rake. Don't till — just scratch the surface. If soil looks dead/compacted, sprinkle a 1/4 inch of compost or topsoil. Do not apply any pre-emergent herbicide — it will prevent your grass seed from germinating too.",
        "when": "Late August (week of Aug 15–25).",
        "duration": "45 min",
    },
    {
        "id": "sep-overseed",
        "month": 9,
        "plantId": "lawn",
        "category": "lawn",
        "title": "Overseed the bare strip (PRIME WINDOW)",
        "what": "Now's the time. The first 2 weeks of September are the absolute best moment in MN for overseeding.",
        "why": "Soil temps still 55–65°F (perfect germination), air cooling, weeds dormant, rains returning. New grass establishes deep roots before frost and explodes in spring.",
        "how": "STEP 1: Mow existing grass short (1.5–2 inches) so seed contacts soil. STEP 2: Pick a grass seed mix matching your conditions — for that shaded strip by the foundation, get a 'sun and shade' or fine fescue blend. Kentucky bluegrass alone is too sun-demanding and slow. STEP 3: Spread seed by hand (small area like yours) — aim for seeds about 1/4 inch apart. STEP 4: Lightly rake to mix seed into the loosened soil. Press in with a foot or roller. STEP 5: Top with a thin straw layer (you should see soil through it — too much smothers). STEP 6: WATER. Light sprinkle 2x/day for the first 2 weeks (keep top 1/2 inch consistently moist; don't let it dry). Once germinated, transition to deeper, less frequent watering. Don't mow until grass is 3+ inches tall (about 4 weeks).",
        "when": "First two weeks of September (Sept 1–15).",
        "duration": "1.5 hours plus 4 weeks of watering",
    },
    {
        "id": "sep-lawn-fertilize",
        "month": 9,
        "plantId": "lawn",
        "category": "lawn",
        "title": "Fall lawn fertilization #1",
        "what": "Apply a fall lawn fertilizer to the rest of the lawn (not the newly seeded strip).",
        "why": "Fall fertilization is the single most important feeding of the year for MN lawns. Roots are actively growing even when top growth slows; nitrogen now builds deep roots and stores energy for an explosive green-up next spring. Skip spring fertilization if you fertilize fall well.",
        "how": "Use a balanced fall lawn fertilizer (look for one that says 'fall' on the bag, typically with potassium). Apply at the rate listed on the bag — too much burns grass. Water in if rain isn't expected. Avoid the just-seeded area for now — the seed has different needs.",
        "when": "Mid-August through mid-September.",
        "duration": "30 min",
    },
    {
        "id": "sep-fall-mulch-leaves",
        "month": 9,
        "plantId": None,
        "category": "general",
        "title": "Plan for fallen leaves (mulch-mow vs. rake)",
        "what": "Decide your strategy for handling the leaves that will start dropping.",
        "why": "Leaves on lawn smother grass. But shredded leaves on beds or mulched into lawn are free fertilizer and soil-building gold.",
        "how": "STRATEGY 1 (recommended for lawn): mulch-mow. Run the mower over fallen leaves on the lawn — it shreds them into tiny pieces that fall between grass blades and decompose by spring. Free organic matter and nitrogen. STRATEGY 2 (for beds): rake leaves from lawn ONTO bed surfaces. They act as winter mulch and break down by summer. STRATEGY 3 (when overwhelmed): bag the excess for yard waste.",
        "when": "Late September through November as leaves fall.",
        "duration": "Ongoing",
    },
    {
        "id": "oct-cut-hostas",
        "month": 10,
        "plantId": "hostas",
        "category": "prune",
        "title": "Cut hostas back to the ground after first hard frost",
        "what": "After the first hard frost turns hosta leaves to mush, cut them all the way down to 2 inches above ground.",
        "why": "Unlike most perennials (which you can leave standing for winter interest), hostas should be cleaned up in fall. Decaying hosta leaves harbor slug eggs and crown rot fungus, and they're attractive shelter for voles that will then chew the crowns over winter.",
        "how": "Wait until first hard frost (usually mid-October in MN). Leaves will be limp and brown. Grab clumps of leaves and snip them with bypass pruners 2 inches above the soil. Bag and dispose (don't compost if any signs of disease). Top the cut crowns with a 2–3 inch layer of shredded leaves or compost for winter insulation.",
        "when": "After first hard frost. Mid-late October.",
        "duration": "20 min for your collection",
    },
    {
        "id": "oct-bring-in-tender",
        "month": 10,
        "plantId": None,
        "category": "general",
        "title": "Pull out spent annuals",
        "what": "Pull begonias and any other annuals once frost kills them.",
        "why": "Decaying annual foliage harbors disease and slugs. Clearing it now means less spring cleanup and a tidier winter view.",
        "how": "Grab each plant at the base and pull. Compost the foliage if disease-free. Save the holes for next year's planting — maybe note locations in this app's notes field.",
        "when": "After first hard frost.",
        "duration": "15 min",
    },
    {
        "id": "oct-water-evergreens",
        "month": 10,
        "plantId": None,
        "category": "water",
        "title": "Deep water evergreens and new plantings before freeze",
        "what": "Give junipers, any other evergreens, and any first-year plantings a deep deep watering before the ground freezes.",
        "why": "Evergreens lose moisture through their needles all winter via transpiration. If they go into winter dry, they get 'winter burn' — brown desiccated foliage by spring. A deep October soak fills their tissues and stores water in the soil they can pull from during winter thaws.",
        "how": "Lay a hose at the base of each evergreen on slow trickle for 20–30 minutes per plant. Do this on a mild day in late October before the ground freezes hard. Don't worry about lawn — cool-season grass handles winter fine.",
        "when": "Mid-late October, before sustained freeze.",
        "duration": "1 hour",
    },
    {
        "id": "nov-rabbit-protection",
        "month": 11,
        "plantId": None,
        "category": "general",
        "title": "Wrap or fence young shrubs against rabbits and voles",
        "what": "Any young or thin-barked shrubs (especially anything planted in the last 3 years) need protection from rabbits and voles that gnaw bark over winter.",
        "why": "Rabbits girdle (chew a complete ring around) young stems at snow level, which kills the stem above the chew. Voles do the same at the soil line under snow. By the time you find the damage in spring it's too late.",
        "how": "Wrap stems with 1/4 inch hardware cloth (NOT chicken wire — voles squeeze through) extending from soil level to 18 inches above expected snow depth. Leave a finger's gap between wrap and stem so it doesn't girdle the plant itself. Remove in April.",
        "when": "Before deep snow arrives, mid-late November.",
        "duration": "30 min depending on # of plants",
    },
    {
        "id": "nov-mulch-perennials",
        "month": 11,
        "plantId": None,
        "category": "general",
        "title": "Mulch perennial crowns after ground freezes",
        "what": "Once the ground freezes solid (not before!), apply 3–4 inches of shredded leaves or straw over perennial crowns.",
        "why": "Counter-intuitive timing: the purpose of winter mulch isn't to keep plants WARM, it's to keep them frozen consistently. Mid-winter thaws followed by freezes are what kills perennials (frost-heaves the crowns). Mulch over frozen ground keeps temperatures stable.",
        "how": "Use shredded leaves, straw, or pine boughs. Apply 3–4 inches deep over the crowns of hostas and any newer perennials. NOT before ground freezes — premature mulching creates a warm haven for voles and slugs.",
        "when": "Late November or early December, after ground is frozen.",
        "duration": "30 min",
    },
    {
        "id": "dec-tool-care",
        "month": 12,
        "plantId": None,
        "category": "general",
        "title": "Clean and sharpen pruning tools",
        "what": "Disassemble, clean, sharpen, and oil your bypass pruners and loppers.",
        "why": "Sharp clean tools make precise cuts that heal fast. Dull tools crush stems and spread disease between plants. Winter is a calm time to do this — and you'll thank yourself in spring.",
        "how": "Wipe sap off blades with mineral spirits or rubbing alcohol. Sharpen the beveled edge of bypass pruners with a small diamond sharpening stone (Felco sells one) at the same angle as the existing bevel — 5–10 strokes per side. Oil all moving parts with light machine oil or 3-in-1. Wipe blades with oil to prevent rust.",
        "when": "Anytime mid-winter, January is perfect.",
        "duration": "30 min",
    },
    {
        "id": "feb-plan",
        "month": 2,
        "plantId": None,
        "category": "general",
        "title": "Garden planning — order seeds, plan changes",
        "what": "Review what worked, what didn't, plan additions and changes for the coming year.",
        "why": "Memory fades. Late winter is when you have time to think, and when garden suppliers have full inventory. By April everyone's slammed and you'll grab what's left.",
        "how": "Walk through this app's notes. What plants struggled? What spots looked empty? Order seeds, plan replacements for invasive honeysuckle removal areas (good MN-native swaps: Serviceberry, Gray Dogwood, Black Chokeberry, or Highbush Cranberry — birds love them and they're not destructive to ecosystems).",
        "when": "February.",
        "duration": "1–2 hours",
    },
]


def seed_for_owner(db, owner_type: str, owner_id: str) -> bool:
    """Seed PLANTS + TASKS for the given owner. Returns True if seeded, False if
    already populated. Idempotent: skips when any rows already exist."""
    existing = db.execute(
        "SELECT COUNT(*) AS n FROM yard_plants WHERE owner_type=? AND owner_id=?",
        (owner_type, owner_id),
    ).fetchone()
    if existing and existing["n"] > 0:
        return False
    for plant in PLANTS:
        db.execute(
            "INSERT INTO yard_plants (id, owner_type, owner_id, data) VALUES (?,?,?,?)",
            (plant["id"], owner_type, owner_id, json.dumps(plant)),
        )
    for task in TASKS:
        db.execute(
            "INSERT INTO yard_tasks (id, owner_type, owner_id, data) VALUES (?,?,?,?)",
            (task["id"], owner_type, owner_id, json.dumps(task)),
        )
    db.commit()
    # Copy default photos into the user's own photo folder.
    _copy_default_photos(owner_id)
    return True


def _cli():
    parser = argparse.ArgumentParser(description="Seed yard data for a user by email.")
    parser.add_argument("--email", required=True, help="Email of an existing user to seed.")
    args = parser.parse_args()

    db_path = Path(__file__).parent / "data" / "mw.db"
    if not db_path.exists():
        print(f"Database not found at {db_path} — run the server once to create it.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT id FROM users WHERE email=?", (args.email.lower(),)).fetchone()
    if not user:
        print(f"No user with email={args.email}", file=sys.stderr)
        sys.exit(2)

    seeded = seed_for_owner(conn, "user", str(user["id"]))
    if seeded:
        print(f"✓ Seeded {len(PLANTS)} plants and {len(TASKS)} tasks for user_id={user['id']} ({args.email}).")
    else:
        print(f"✓ User {args.email} already has yard data — nothing to do (idempotent).")
    conn.close()


if __name__ == "__main__":
    _cli()
