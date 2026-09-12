import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export function DiscoverPanel() {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-border bg-card px-4 py-3">
        <p className="text-sm text-muted-foreground">
          Subnets: 192.168.1.0/24 · 10.0.0.0/24
        </p>
        <div className="flex w-full flex-wrap items-end gap-2">
          <Button type="button">Scan for K1</Button>
          <Button type="button" variant="outline" disabled>
            Stop
          </Button>
          <FieldGroup className="flex-row items-end gap-2">
            <Field className="gap-1.5">
              <FieldLabel htmlFor="k1-ip">Or enter K1 IP</FieldLabel>
              <Input id="k1-ip" defaultValue="192.168.1.81" className="w-40 font-mono" />
            </Field>
            <Button type="button" variant="outline">
              Verify
            </Button>
          </FieldGroup>
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1 px-4 py-2">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Confidence</TableHead>
              <TableHead>IP Address</TableHead>
              <TableHead>Hostname</TableHead>
              <TableHead>SSH Banner</TableHead>
              <TableHead>Why</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className="font-mono text-xs">
            <TableRow>
              <TableCell>
                <Badge>High</Badge>
              </TableCell>
              <TableCell>192.168.1.81</TableCell>
              <TableCell>booster-k1</TableCell>
              <TableCell>OpenSSH_8.9</TableCell>
              <TableCell className="text-muted-foreground">banner + hostname match</TableCell>
            </TableRow>
            <TableRow>
              <TableCell>
                <Badge variant="secondary" className="text-warning">
                  Medium
                </Badge>
              </TableCell>
              <TableCell>192.168.1.44</TableCell>
              <TableCell>orin-dev</TableCell>
              <TableCell>OpenSSH_8.2</TableCell>
              <TableCell className="text-muted-foreground">open 22 only</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </ScrollArea>

      <div className="flex flex-wrap gap-2 border-t border-border px-4 py-3">
        <Button type="button" variant="secondary">
          Verify Selected
        </Button>
        <Button type="button" variant="outline">
          Use this IP everywhere
        </Button>
      </div>
      <pre className="mx-4 mb-4 h-36 overflow-auto border border-border bg-secondary/40 p-3 font-mono text-xs leading-relaxed text-primary">
{`--- Starting scan ---
[+] 192.168.1.81  High  booster-k1
ready.`}
      </pre>
    </div>
  )
}
