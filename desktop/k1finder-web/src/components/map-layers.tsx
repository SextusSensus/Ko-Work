import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { MapLayers } from "@/lib/localmap"

const LAYER_META: { id: keyof MapLayers; label: string }[] = [
  { id: "env", label: "Environment" },
  { id: "occ", label: "Occupancy" },
  { id: "robot", label: "Robot" },
  { id: "trail", label: "Odom trail" },
  { id: "follow", label: "Follow pose" },
  { id: "exterior", label: "Exterior" },
]

type Props = {
  layers: MapLayers
  onToggle: (id: keyof MapLayers, on: boolean) => void
}

export function MapLayersPanel({ layers, onToggle }: Props) {
  const value = LAYER_META.filter((l) => layers[l.id]).map((l) => l.id)

  return (
    <ToggleGroup
      type="multiple"
      orientation="vertical"
      variant="outline"
      size="sm"
      spacing={1}
      value={value}
      onValueChange={(next) => {
        for (const meta of LAYER_META) {
          const on = next.includes(meta.id)
          if (on !== layers[meta.id]) onToggle(meta.id, on)
        }
      }}
      aria-label="Layer toggles"
      className="w-[148px] items-stretch"
    >
      {LAYER_META.map((meta) => (
        <ToggleGroupItem
          key={meta.id}
          value={meta.id}
          className="justify-start rounded-sm px-3 font-semibold tracking-[0.08em] uppercase data-[state=on]:border-primary/55 data-[state=on]:bg-primary/10 data-[state=on]:text-foreground"
          title={
            meta.id === "exterior"
              ? "Show/hide building envelope walls. Default ON."
              : undefined
          }
        >
          <span
            className="mr-2 inline-block size-1.5 rounded-full bg-muted-foreground data-[state=on]:bg-primary"
            data-state={layers[meta.id] ? "on" : "off"}
            aria-hidden
          />
          {meta.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}
