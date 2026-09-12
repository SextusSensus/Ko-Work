# Sky Connect web (shadcn/ui)

Vite + React + Tailwind v4 + **shadcn/ui** operator chrome for Sky Connect. Theme tokens: OLED blacks, star-white primary, launch-blue accent.

Local Map embeds `../localmap-viewer` (Three.js + OrbitControls) with `?embed=1`. Drive safety stays in `K1Finder.ps1`.

## Run

```bash
npm install
npm run dev      # http://127.0.0.1:5173/?tab=map
npm run build    # dist/ → WebView2 host prefers this
npm run preview
```

See [`../README-UI.md`](../README-UI.md) for domains, telemetry bridge, and WinForms hosting.
