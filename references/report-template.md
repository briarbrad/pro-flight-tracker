# HTML Email Report Template

Use this structure for every flight check email. Adapt content but preserve
the layout hierarchy — passengers scan from top to bottom and want the
verdict instantly.

## Subject Line
```
✈️ DL960 LAX→JFK — Aug 13, 2026 — Risk: 🟢 LOW
```

## HTML Template

```html
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 640px; margin: 0 auto; padding: 16px; color: #1a1a1a; }
  .header { background: #1a237e; color: white; padding: 20px; border-radius: 12px 12px 0 0; }
  .header h1 { margin: 0; font-size: 24px; }
  .header .route { font-size: 18px; opacity: 0.9; margin-top: 4px; }
  .header .meta { font-size: 14px; opacity: 0.7; margin-top: 8px; }
  .verdict { padding: 24px; text-align:center; border-radius: 0 0 12px 12px; margin-bottom: 16px; }
  .verdict-green { background: #e8f5e9; border: 2px solid #4caf50; }
  .verdict-yellow { background: #fff8e1; border: 2px solid #ff9800; }
  .verdict-red { background: #ffebee; border: 2px solid #f44336; }
  .verdict .emoji { font-size: 48px; }
  .verdict .level { font-size: 28px; font-weight: bold; margin-top: 8px; }
  .verdict .summary { font-size: 16px; margin-top: 12px; color: #424242; }
  .section { margin-bottom: 16px; border: 1px solid #e0e0e0; border-radius: 8px; overflow: hidden; }
  .section-title { background: #f5f5f5; padding: 12px 16px; font-weight: 600; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #e0e0e0; }
  .section-body { padding: 16px; }
  table.risk { width: 100%; border-collapse: collapse; }
  table.risk td { padding: 10px 12px; border-bottom: 1px solid #f0f0f0; }
  table.risk td:first-child { font-weight: 600; width: 50%; }
  table.risk td:last-child { text-align: right; }
  .risk-green { color: #2e7d32; }
  .risk-yellow { color: #e65100; }
  .risk-red { color: #c62828; }
  .chain-item { display: flex; align-items: center; padding: 8px 0; border-bottom: 1px solid #f5f5f5; }
  .chain-icon { font-size: 20px; margin-right: 12px; }
  .chain-detail { flex: 1; }
  .chain-label { font-size: 12px; color: #757575; }
  .chain-value { font-size: 15px; font-weight: 500; }
  .wx-raw { background: #fafafa; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; padding: 12px; border-radius: 6px; white-space: pre-wrap; word-break: break-word; margin-top: 8px; color: #37474f; }
  .comparison { width: 100%; border-collapse: collapse; margin-top: 8px; }
  .comparison th { text-align: left; padding: 6px 8px; background: #f5f5f5; font-size: 13px; }
  .comparison td { padding: 6px 8px; font-size: 14px; border-bottom: 1px solid #f0f0f0; }
  .change-up { color: #c62828; font-weight: 600; }
  .change-down { color: #2e7d32; font-weight: 600; }
  .change-same { color: #757575; }
  .footer { text-align: center; font-size: 12px; color: #9e9e9e; margin-top: 24px; padding-top: 16px; border-top: 1px solid #e0e0e0; }
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <h1>✈️ DL960</h1>
  <div class="route">LAX → JFK</div>
  <div class="meta">Wednesday, August 13, 2026 · Boeing 767-400ER (N178DZ)</div>
</div>

<!-- RISK VERDICT -->
<div class="verdict verdict-green">
  <div class="emoji">🟢</div>
  <div class="level">LOW RISK</div>
  <div class="summary">Your flight looks good. Clear skies at JFK for your 6:30 AM arrival, equipment chain is solid, and no active delay programs affect this flight.</div>
</div>

<!-- RISK TABLE -->
<div class="section">
  <div class="section-title">Risk Assessment</div>
  <div class="section-body">
    <table class="risk">
      <tr><td>🛫 Departure Delay</td><td class="risk-green">🟢 LOW</td></tr>
      <tr><td>✈️ En Route</td><td class="risk-green">🟢 LOW</td></tr>
      <tr><td>🛬 Arrival Conditions</td><td class="risk-green">🟢 LOW</td></tr>
      <tr><td>🔧 Equipment Chain</td><td class="risk-green">🟢 LOW</td></tr>
    </table>
  </div>
</div>

<!-- BOTTOM LINE -->
<div class="section">
  <div class="section-title">Bottom Line</div>
  <div class="section-body">
    <p>[One paragraph plain-English summary for the passenger. Be honest and specific.]</p>
  </div>
</div>

<!-- EQUIPMENT CHAIN -->
<div class="section">
  <div class="section-title">Equipment Chain</div>
  <div class="section-body">
    <div class="chain-item">
      <div class="chain-icon">🔧</div>
      <div class="chain-detail">
        <div class="chain-label">Aircraft</div>
        <div class="chain-value">N178DZ · Boeing 767-400ER</div>
      </div>
    </div>
    <div class="chain-item">
      <div class="chain-icon">🛬</div>
      <div class="chain-detail">
        <div class="chain-label">Inbound Flight</div>
        <div class="chain-value">DL123 from ATL · On time · ETA 9:15 PM</div>
      </div>
    </div>
    <div class="chain-item">
      <div class="chain-icon">⏱️</div>
      <div class="chain-detail">
        <div class="chain-label">Turn Time</div>
        <div class="chain-value">135 min available · 90 min standard for widebody ✅</div>
      </div>
    </div>
  </div>
</div>

<!-- DEPARTURE WEATHER -->
<div class="section">
  <div class="section-title">Departure Weather — LAX</div>
  <div class="section-body">
    <p>[Analysis of departure airport conditions]</p>
    <div class="wx-raw">[Raw METAR]
[TAF excerpt for departure window]</div>
  </div>
</div>

<!-- DESTINATION WEATHER AT ARRIVAL WINDOW -->
<div class="section">
  <div class="section-title">Destination Weather at Arrival — JFK (6:30 AM ET)</div>
  <div class="section-body">
    <p>[Projected conditions at the actual arrival time, NOT current conditions]</p>
    <div class="wx-raw">[Raw METAR (current, for reference)]
[TAF excerpt for arrival window]</div>
    <p><strong>Beacon Ensemble:</strong> [Precipitation forecast with model agreement]</p>
  </div>
</div>

<!-- FAA / GDP CASCADE ANALYSIS -->
<div class="section">
  <div class="section-title">FAA / GDP Cascade Analysis</div>
  <div class="section-body">
    <p>[Branch A/B classification, trend, impact assessment]</p>
  </div>
</div>

<!-- TIMESTAMPS -->
<div class="footer">
  Data pulled: Aug 13, 2026 at 11:28 AM ET<br>
  Next recommended recheck: [time]
</div>

</body>
</html>
```

## Recheck Comparison Table

For rechecks, add this section between RISK TABLE and BOTTOM LINE:

```html
<div class="section">
  <div class="section-title">Changes Since Last Check</div>
  <div class="section-body">
    <table class="comparison">
      <tr><th>Factor</th><th>Morning</th><th>Now</th><th>Change</th></tr>
      <tr>
        <td>Departure Risk</td>
        <td>🟢 LOW</td>
        <td>🟡 MODERATE</td>
        <td class="change-up">↑ Elevated</td>
      </tr>
      <tr>
        <td>JFK GDP</td>
        <td>None</td>
        <td>Active (avg 45 min)</td>
        <td class="change-up">⚠️ New</td>
      </tr>
      <tr>
        <td>Equipment Chain</td>
        <td>On time</td>
        <td>On time</td>
        <td class="change-same">— Same</td>
      </tr>
    </table>
  </div>
</div>
```

## Color Mapping

| Risk Level | Verdict Class | Emoji | Text Color Class |
|---|---|---|---|
| LOW | verdict-green | 🟢 | risk-green |
| MODERATE | verdict-yellow | 🟡 | risk-yellow |
| HIGH | verdict-red | 🔴 | risk-red |
