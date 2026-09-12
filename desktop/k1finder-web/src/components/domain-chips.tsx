import * as React from "react"
import { PlusIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { MapDomain } from "@/lib/localmap"
import { cn } from "@/lib/utils"

type Props = {
  domains: MapDomain[]
  activeId: string | null
  onSelect: (id: string) => void
  onCreate: (name: string) => void | Promise<void>
}

export function DomainChips({ domains, activeId, onSelect, onCreate }: Props) {
  const [open, setOpen] = React.useState(false)
  const [name, setName] = React.useState("")
  const [pending, setPending] = React.useState(false)

  async function submit() {
    const trimmed = name.trim()
    if (!trimmed) return
    setPending(true)
    try {
      if (window.k1LocalMapHostCreateDomain) {
        window.k1LocalMapHostCreateDomain({ name: trimmed })
      }
      await onCreate(trimmed)
      setOpen(false)
      setName("")
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex min-w-0 flex-1 items-center gap-2.5 overflow-x-auto py-0.5">
      {domains.map((d) => {
        const active = d.id === activeId
        return (
          <Button
            key={d.id}
            type="button"
            size="sm"
            variant={active ? "default" : "outline"}
            aria-pressed={active}
            title={`${d.name || d.id} · ${d.cell_count || 0} cells · ${d.run_count || 0} runs`}
            onClick={() => onSelect(d.id)}
            className={cn(
              "h-9 shrink-0 cursor-pointer rounded-full px-4 transition-all duration-200",
              active && "shadow-[0_0_0_1px_rgb(255_255_255/0.18)]"
            )}
          >
            {d.name || d.id}
          </Button>
        )
      })}
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="h-9 shrink-0 cursor-pointer rounded-full border-dashed border-white/15 px-4 text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(true)}
      >
        <PlusIcon data-icon="inline-start" />
        New
      </Button>
      <Badge
        variant="outline"
        className="ml-auto hidden shrink-0 rounded-full px-3 py-1 font-mono text-[10px] tracking-wide sm:inline-flex"
      >
        domains
      </Badge>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="rounded-3xl sm:max-w-md">
          <DialogHeader className="gap-2">
            <DialogTitle>New domain</DialogTitle>
            <DialogDescription className="leading-relaxed">
              Separate map for a new environment. Follow / capture runs merge into
              the active domain over time. New domains start empty.
            </DialogDescription>
          </DialogHeader>
          <FieldGroup className="gap-4 py-2">
            <Field className="gap-2">
              <FieldLabel htmlFor="domain-name">Domain name</FieldLabel>
              <Input
                id="domain-name"
                value={name}
                maxLength={64}
                placeholder="e.g. Warehouse Bay B"
                className="h-11 rounded-xl font-mono"
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void submit()
                }}
              />
            </Field>
          </FieldGroup>
          <DialogFooter className="gap-2">
            <Button
              type="button"
              variant="outline"
              className="h-10 rounded-xl"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl"
              disabled={pending || !name.trim()}
              onClick={() => void submit()}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
