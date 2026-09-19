export type AutotuneSource = "mock" | "feed"

export type AutotuneColumn = "robot" | "trainer" | "queue"

export type AutotuneTone = "accent" | "success" | "danger" | "muted"

export type AutotuneBadge = "PASS" | "FAIL" | "HOLD" | "INCONCLUSIVE"

export type AutotuneCard = {
  id: string
  column: AutotuneColumn
  title: string
  body: string
  meta?: string
  tone?: AutotuneTone
  badge?: AutotuneBadge
}

export type AutotuneProgress = {
  label: string
  v: number
  max: number
  hint?: string
}

export type AutotuneSnapshot = {
  source?: AutotuneSource
  progress?: AutotuneProgress | null
  cards: AutotuneCard[]
  activity: string[]
}

export const COLUMNS: {
  id: AutotuneColumn
  label: string
  hint: string
}[] = [
  {
    id: "robot",
    label: "To robot",
    hint: "Hints + PASS bundles in flight or on device",
  },
  {
    id: "trainer",
    label: "Trainer / labelling",
    hint: "GPU labeller and recording pulls",
  },
  {
    id: "queue",
    label: "Queue",
    hint: "Waiting, analyse, retries, held, gate results",
  },
]

/** Designed Autotune Viz mock — matches the improve-backend board. */
export const MOCK_SNAPSHOT: AutotuneSnapshot = {
  source: "mock",
  progress: {
    label: "Labelling 20260912T041208Z_k1 · GPU pass 2/3",
    v: 2,
    max: 3,
    hint: "in flight",
  },
  cards: [
    {
      id: "20260912T033108Z_k1",
      column: "robot",
      title: "In flight → robot",
      tone: "accent",
      badge: "PASS",
      body: "PASS — waiting to send PASS bundle to robot",
      meta: "PASS · 14 unique obstacles · clearance OK",
    },
    {
      id: "20260911T225158Z_k1",
      column: "robot",
      title: "Hints on robot",
      tone: "success",
      body: "Tune hints ledgered — advisory YAML on robot",
    },
    {
      id: "20260912T041208Z_k1",
      column: "trainer",
      title: "Labelling now",
      tone: "accent",
      body: "GPU labeller active · 18432.6382910012345 90",
    },
    {
      id: "20260912T020045Z_k1",
      column: "queue",
      title: "Awaiting analyse",
      body: "Pulled run waiting for Auto-Tune analyse",
    },
    {
      id: "20260910T180012Z_k1",
      column: "queue",
      title: "Gate result",
      tone: "danger",
      badge: "FAIL",
      body: "Gate FAIL · gate v3",
      meta: "FAIL · floor tilt out of band",
    },
  ],
  activity: [
    "stage: labelling 20260912T041208Z_k1 on the GPU (attempt 2, cap 90 min)",
    "rrd: pull complete 842 MB → depth_replay.json",
    "hints: scp tune_patch.yaml → /home/booster/tune_hints/latest.yaml",
    "loop: robot idle · no operator session · tick",
  ],
}

export function cardsIn(
  snapshot: AutotuneSnapshot,
  column: AutotuneColumn
): AutotuneCard[] {
  return snapshot.cards.filter((card) => card.column === column)
}

export function isAutotuneSnapshot(value: unknown): value is AutotuneSnapshot {
  if (!value || typeof value !== "object") return false
  const snap = value as AutotuneSnapshot
  return Array.isArray(snap.cards) && Array.isArray(snap.activity)
}
