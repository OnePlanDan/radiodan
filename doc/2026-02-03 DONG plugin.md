# Dong! Plugin — Time-Based Announcement Plugin

## Goal
Create a "Dong!" plugin that fires announcements based on time/schedule.
Three mutually exclusive modes, two content fields, plus new `select` and
`datetime` form field types for the shared GUI infrastructure.

**Default**: recurring hourly on the dot, saying "Dooong! The time is HH:MM"

---

## Step 1: New Form Field Types (shared infra)

Before building the plugin, extend the form system with two new types any
plugin can use.

### 1a. Template — `instance_form.html`

Add two new `{% elif %}` blocks in the field-type dispatch (between `bool`
and `style_picker`):

**`select`** → `<select class="input">` with `<option>` loop, `selected`
on match.

**`datetime`** → `<input type="datetime-local" class="input">`.

Both emit `data-show-when-field` / `data-show-when-value` attributes from
an optional `field.show_when` dict, enabling conditional visibility.

Also add `data-show-when-*` to **all** existing field-type blocks (text,
number, bool, textarea) so any plugin can use conditional visibility.

Add a `<script>` at the end of `{% block content %}`:
```js
// On load + on change: for each [data-show-when-field] element,
// show it if the referenced field's value matches, else hide it.
// Uses .field-hidden { display: none } CSS class.
```

### 1b. Routes — `plugins.py`

Add `select` and `datetime` branches in `_parse_form_fields()`:
```python
elif ftype in ("select", "datetime"):
    config[key] = data.get(f"field__{key}", field.get("default", ""))
```
`_prepare_config_fields()` needs no changes — the default `else` branch
already handles them.

### 1c. CSS — `style.css`

```
select.input        — appearance:none, custom ▼ arrow SVG, dark options
datetime-local.input — color-scheme:dark
.form-group.field-hidden — display:none
```

---

## Step 2: Create `bridge/plugins/dong.py`

### Class skeleton
```python
@register_plugin
class DongPlugin(DJPlugin):
    name = "dong"
    description = "Time-based announcements — hourly chimes, scheduled alerts, per-song"
    version = "0.1.0"
```

### config_fields() — 7 fields

| Key | Type | Default | Label | show_when |
|-----|------|---------|-------|-----------|
| `active_on_start` | bool | `True` | Active on Start | — |
| `mode` | select | `"recurring"` | Mode | — |
| `recurring_type` | select | `"hourly"` | Recurring Schedule | mode=recurring |
| `daily_time` | text | `"12:00"` | Daily Time (HH:MM) | recurring_type=daily |
| `oneshot_datetime` | datetime | `""` | Fire At | mode=oneshot |
| `say_text` | text | `"Dooong! The time is {time}"` | Say Text | — |
| `prompt` | textarea | `""` | LLM Prompt (fallback) | — |

Options for `mode`: recurring, oneshot, between_songs
Options for `recurring_type`: hourly, daily

### on_start() — mode dispatch

```
Read config → set self._active, self._mode, self._say_text, self._prompt

if not active: return early

mode == "recurring":
  recurring_type == "hourly" → create_task(_clock_aligned_loop(60, minute=0))
  recurring_type == "daily"  → create_task(_daily_loop(hour, minute))

mode == "oneshot":
  parse oneshot_datetime → create_task(_oneshot_fire(target))

mode == "between_songs":
  stream_context.on("track_changed", _on_track_changed)
```

### Clock-aligned scheduling

**`_clock_aligned_loop(interval_minutes, target_minute)`**:
```
loop:
  now = datetime.now()
  next_fire = now.replace(minute=target_minute, second=0, microsecond=0)
  if next_fire <= now: next_fire += timedelta(minutes=interval_minutes)
  await asyncio.sleep((next_fire - now).total_seconds())
  if active and running: await _fire_announcement()
```

**`_daily_loop(hour, minute)`**:
```
loop:
  now = datetime.now()
  next_fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
  if next_fire <= now: next_fire += timedelta(days=1)
  await asyncio.sleep(delay)
  if active and running: await _fire_announcement()
```

**`_oneshot_fire(dt_str)`**:
```
target = fromisoformat(dt_str)
delay = (target - now).total_seconds()
if delay <= 0: log warning, return
await asyncio.sleep(delay)
if active and running: await _fire_announcement()
```

**`_on_track_changed(track_info)`**: if active → _fire_announcement()

### _fire_announcement()

```
time_str = datetime.now().strftime("%H:%M")

if say_text is non-empty:
    text = say_text.replace("{time}", time_str)
    await self.say(text, trigger="between_songs", priority=30)
elif prompt is non-empty:
    prompt_filled = prompt.replace("{time}", time_str)
    text = await self.ctx.llm_service.chat(prompt_filled)
    await self.say(text, trigger="between_songs", priority=30)
```

### Telegram integration

- Menu button: `🔔 {display_name} ON` / `🔔 {display_name}`
- Toggle callback: flip `self._active`, return status with mode + text preview
- Same pattern as presenter plugin

---

## Files Changed

| File | Change |
|------|--------|
| `bridge/plugins/dong.py` | **NEW** — full plugin (~130 lines) |
| `bridge/web/templates/plugins/instance_form.html` | Add `select` + `datetime` blocks, `data-show-when` on all fields, visibility JS |
| `bridge/web/routes/plugins.py` | Add `select`/`datetime` parsing in `_parse_form_fields()` |
| `bridge/web/static/style.css` | `select.input`, datetime dark-scheme, `.field-hidden` |

---

## Verification

```bash
# 1. No syntax errors
uv run python -c "import bridge.main; print('OK')"

# 2. Config fields structure
uv run python -c "
from bridge.plugins.dong import DongPlugin
fields = DongPlugin.config_fields()
print(f'{len(fields)} fields defined')
for f in fields:
    sw = f' (show_when: {f[\"show_when\"]})' if f.get('show_when') else ''
    print(f'  {f[\"key\"]}: {f[\"type\"]}{sw}')
"

# 3. Start app → /plugins → create a Dong instance → click Edit
#    Should see: mode dropdown, conditional fields appearing/hiding,
#    say_text with default "Dooong! The time is {time}"
#    Select "One-shot" → datetime picker appears, recurring fields hide
#    Select "Between every song" → both recurring + oneshot fields hide

# 4. Save → reload → values persist

# 5. Telegram: /menu → should show 🔔 toggle button
```
