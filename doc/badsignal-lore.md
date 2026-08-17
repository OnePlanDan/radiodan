# Bad Signal After Dark — sponsor lore origin

Ideated with Dan 2026-08-16/17. This is the origin document for the show's
commercial universe. Canon status: **Glottco** and **Rundqvist & Son** are
already in the live show bible; the three below are approved in spirit but not
yet written into canon — pending the AudioSegment "brands as first-class"
answer (see the wish in `journal.md`, 2026-08-17), which may change where this
lore lives.

The formula: a real 2026 trend pushed exactly one step too far; a founder whose
backstory explains too much; a disclaimer that quietly confesses the crime;
details that recur and mutate across episodes.

## The roster

### Glottco *(canon)*
The megacorp. Makes everything, explains nothing. Its slogan is different every
time and nobody at Glottco acknowledges this. Acquires things and declines to
be named. Its customer-service hold music may yet become a recurring
interstitial — the estimated wait grows every appearance.

### Rundqvist & Son *(canon)*
Gothenburg discount aquarium, also a notary. Founded 1962 or 1997 depending on
which ad you heard. The son has never been seen. The only notary open at night
in Gothenburg — which other sponsors quietly depend on.

### HemVakt™ *(approved, not yet canon)*
AI entry system: greets guests by name, quietly scores them; premium tier
pre-emptively turns away people you would not have enjoyed. Slogan: **"Ditt hem
vet."** Founded by Roland Vakt, ex-bouncer from Hisingen, who claimed he could
tell everything about a person from how they knock; acquired after fourteen
months by a company that declines to be named (a Glottco-shaped silence).
Nightly firmware updates whose release notes nobody has read.
Fine print: *"HemVakt may decline entry to residents whose demeanor deviates
from baseline. Baseline is proprietary."*
Lore hooks: Roland Vakt has not been observed entering his own home since
March. Duke will conclude the station's greeter is a HemVakt beta unit. The
HemVakt ad announcer drifts, over episodes, into greeting the listener by name.

### Drowze™ *(approved, not yet canon)*
Sleep as a subscription. Tiers: Doze (six hours, REM throttled at peak),
Standard, Premium ("dream in colour"). Overage fees for napping; the family
plan shares a sleep pool, making insomnia transferable to your children.
Founded by Margit Öhrn, former airline seat-economist: beds were "criminally
under-monetized real estate." Slogan mutates: *"You'll sleep when we let
you."* / *"Rest assured — terms apply."*
Fine print: *"Drowze may downsample dreams during peak hours. Recurring
characters in dreams may be sponsored. Waking exhausted does not constitute a
service failure."*
Lore hooks: Duke refuses on principle; Nyx has Premium and describes
suspiciously specific sponsored dreams. Ads include a "free sample of Premium
Sleep": four seconds of actual dead air.

### Strax® *(approved, not yet canon)*
Predictive delivery: the parcel arrives before you knew you wanted it. Returns
require Form 11-B, sworn and notarized — and the only notary open at night is
Rundqvist & Son. Began as a rounding error in a logistics forecasting model
that kept shipping unordered goods; renamed a feature, raised a round. There is
no founder — press releases are signed *"Strax, itself."*
Fine print: *"Acceptance of a Strax parcel constitutes retroactive intent to
purchase. Unopened parcels accrue curiosity fees."*
Lore hooks: a Strax parcel arrives for Duke on air, unordered; its contents pay
off episodes later.

### The bench
EverKin (grief-tech: an AI trained on your relatives so you never have to call
them — possibly too dark), Mynt (spare change invested in "whatever is angriest
online today"), NutriLoop (edible packaging; leftovers become next week's box —
"circularity you can taste").

## The production-trick toolbox

Speed and time: fine print at 3x with one sentence dropped to half speed for
legal emphasis; before/after customer voices at 0.9x/1.1x; the free sample of
silence (dead air as product demo).

Voice and disguise: testimonials by the other host in a terrible disguise,
which nobody acknowledges; one mundane word per ad in the monster-truck-rally
voice; the shouting ad for a boring product; announcer drift (the HemVakt
announcer slowly becomes the product).

Structure: the ad interrupted mid-slogan that resumes mid-syllable twenty
minutes later; the phone number read twice, differently, both wrong; the glossy
national spot followed by a hastily-recorded local tag in awful quality; the
disclaimer that has its own sponsor; the recall notice whose original
disclaimer is read in reverse at 3x; the ad that pauses when Duke heckles it
and continues slightly colder.

Feasibility notes: fast/slow/shouty deliveries work today via per-segment
`instruct`; true time-stretch works today for the standalone spot library (we
slice those ourselves — ffmpeg atempo per spot); true per-segment speed inside
episodes needs AudioSegment support (unconfirmed); AudioSegment's
`sfx_prompts` feature flag can render sound-effect segments (doorbells, trucks
reversing, emergency-broadcast tones) via Stable Audio.
