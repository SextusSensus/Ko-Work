# Design System Master File

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** K1 Finder  
**Led by:** UI/UX Pro Max  
**Category:** Space Tech / Aerospace × Liquid Glass × Bento  
**Updated:** 2026-09-12  

---

## Direction

Quiet SpaceX mission-control chrome: deep OLED blacks, star-white primary actions,
launch-blue used sparingly for status, soft glass panels, generous breathing room.

**Avoid:** neon cyan, harsh hairlines, cramped toolbars, loud gradients, emoji icons.

## Spacing rhythm

| Token | Value | Use |
|-------|-------|-----|
| Page inset | `20–32px` | Shell padding |
| Section gap | `20–24px` | Between bento blocks |
| Panel pad | `20–24px` | Inside glass cards |
| Control height | `36–44px` | Buttons / inputs |
| Chip height | `36px` | Domain pills |
| Radius | `16–28px` | Cards `rounded-3xl`, chips `rounded-full` |

## Color Palette

| Role | Hex | CSS Variable |
|------|-----|--------------|
| Primary | `#FAFAFA` | `--primary` |
| On Primary | `#0A0A0F` | `--primary-foreground` |
| Accent | `#3B82F6` | `--accent` |
| Background | `#07070A` | `--background` |
| Card | `#121218` | `--card` |
| Muted Foreground | `#A1A1AA` | `--muted-foreground` |
| Border | `rgb(255 255 255 / 0.08)` | `--border` |
| Destructive | `#EF4444` | `--destructive` |

## Typography

- **UI / Headings:** Exo
- **Mono / telemetry:** Roboto Mono
- Title ~32–36px semibold tracking tight; body 14–15px relaxed

## Effects

- Glass: translucent card + `backdrop-blur` + soft inset highlight
- Motion: 150–250ms opacity/color; respect `prefers-reduced-motion`
- No neon glow rings

## Pre-delivery

- [ ] Cursor-pointer on clickables
- [ ] Touch targets ≥ 36px, gaps ≥ 8px
- [ ] Contrast ≥ 4.5:1
- [ ] Reduced motion honored
