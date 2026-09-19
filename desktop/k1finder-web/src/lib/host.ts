// Sky Connect host bridge (WebView2).
//
// The web shell is a skin over the classic K1Finder.ps1 controls, which stay the single source of truth.
// Every action here asks the host to drive the SAME WinForms control and handler (Start-Scan, Do-Verify,
// Start-Tracker, Stop-Tracker, the ARM confirmation, ...), so the launch pre-flight, the operator-session
// contract, the confirmations and STOP behave exactly as in the classic app. The host pushes back a snapshot
// of those controls, the scan results, the logs and the Tracker preview frame.
//
// Messages to the host:   {t:"hello"} | {t:"click", id} | {t:"set", id, v} | {t:"row", i, verify}
// Messages from the host: {t:"state", ...HostState} | {t:"log", ch, text, reset} | {t:"frame", src} | {t:"autotune", ...AutotuneSnapshot}
import * as React from "react"

import { type AutotuneSnapshot } from "@/lib/autotune"

export type Ctl = {
  e: boolean // enabled
  k?: boolean // checked (CheckBox)
  x?: string // text (CheckBox / Button / Label)
  v?: string | number // value (TextBox / ComboBox / TrackBar)
  o?: string[] // options (ComboBox)
  min?: number
  max?: number
}

export type Row = {
  c: string
  ip: string
  h: string
  b: string
  w: string
  sel: boolean
}

export type HostState = {
  c: Record<string, Ctl>
  rows: Row[]
  status: string
  progress: { v: number; max: number }
  subnets: string
  scanning: boolean
  trackOn: boolean
  badge: { x: string; bg: string }
  reid: { x: string; bg: string }
  dist: string
  spd: string
}

type WebView = {
  postMessage: (message: unknown) => void
  addEventListener: (
    type: "message",
    listener: (event: MessageEvent) => void
  ) => void
}

function webview(): WebView | null {
  const w = window as unknown as { chrome?: { webview?: WebView } }
  return w.chrome?.webview ?? null
}

let hostState: HostState | null = null
const logs: Record<string, string> = { discover: "", tracker: "" }
let frame: string | null = null
let autotune: AutotuneSnapshot | null = null
let version = 0
let started = false
const listeners = new Set<() => void>()

function asArray<T>(value: T | T[] | null | undefined): T[] {
  if (value == null) return []
  return Array.isArray(value) ? value : [value]
}

function emit() {
  version++
  listeners.forEach((listener) => listener())
}

function start() {
  if (started) return
  started = true
  const view = webview()
  if (!view) return
  view.addEventListener("message", (event) => {
    const m = event.data as { t?: string } & Record<string, unknown>
    if (!m || typeof m !== "object") return
    if (m.t === "state") {
      hostState = m as unknown as HostState
      emit()
    } else if (m.t === "log") {
      const ch = String(m.ch ?? "")
      const text = String(m.text ?? "")
      logs[ch] = (m.reset ? text : (logs[ch] ?? "") + text).slice(-20000)
      emit()
    } else if (m.t === "frame") {
      frame = typeof m.src === "string" ? m.src : null
      emit()
    } else if (m.t === "autotune") {
      const raw = m as AutotuneSnapshot
      autotune = {
        source: "feed",
        progress: raw.progress ?? null,
        cards: asArray(raw.cards),
        activity: asArray(raw.activity).map((line) => String(line)),
      }
      emit()
    }
  })
  view.postMessage({ t: "hello" })
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function send(message: Record<string, unknown>) {
  webview()?.postMessage(message)
}

/** Press a classic Button (by its K1Finder.ps1 variable name). */
export const click = (id: string) => send({ t: "click", id })
/** Set a classic CheckBox / ComboBox / TrackBar / TextBox (its change handler runs as if the user did it). */
export const setValue = (id: string, v: unknown) => send({ t: "set", id, v })
/** Select a Discover candidate row; verify=true also runs Verify on it. */
export const selectRow = (i: number, verify = false) =>
  send({ t: "row", i, verify })

export function useHost() {
  start()
  React.useSyncExternalStore(subscribe, () => version)
  return {
    state: hostState,
    logs,
    frame,
    autotune,
    available: webview() !== null,
  }
}

/** A text box that follows the host value, except while the user is typing in it. */
export function useLiveInput(hostValue: string | undefined) {
  const [value, setLocal] = React.useState(hostValue ?? "")
  const [editing, setEditing] = React.useState(false)
  React.useEffect(() => {
    if (!editing && hostValue !== undefined) setLocal(hostValue)
  }, [hostValue, editing])
  return { value, setLocal, editing, setEditing }
}
