import { useEffect, useState } from "react";
import { ScrollText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatLatency, formatRelativeTime, formatTimestamp } from "../format";
import type { RequestLogPage } from "../types";
import { PaginationControls } from "./pagination-controls";
import { TableSkeleton } from "./skeletons";

export type RequestLogTableProps = {
  page: RequestLogPage;
  isLoading: boolean;
  onLimitChange: (limit: number) => void;
  onOffsetChange: (offset: number) => void;
};

export function RequestLogTable({ page, isLoading, onLimitChange, onOffsetChange }: RequestLogTableProps) {
  const isEmpty = !isLoading && page.total === 0;
  // One shared "now" for every row, ticked coarsely so a long-lived tab does
  // not freeze at "just now"; the lazy initializer keeps the impure Date.now()
  // out of the render body.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent proxy requests</CardTitle>
        <CardDescription>
          Metadata only — prompts, completions, API keys, and Kiro tokens are never stored.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={Math.min(page.limit, 5)} columns={5} />
        ) : isEmpty ? (
          <EmptyState icon={ScrollText} title="No requests recorded yet" description="Proxy traffic will appear here." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Route</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden text-right md:table-cell">Latency</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.logs.map((log, index) => (
                <TableRow key={`${log.created_at}-${page.offset + index}`}>
                  <TableCell title={formatTimestamp(log.created_at)}>
                    {formatRelativeTime(now, log.created_at)}
                  </TableCell>
                  <TableCell className="max-w-[10rem] truncate font-mono text-xs md:max-w-none">{log.route}</TableCell>
                  <TableCell>{log.model ?? "—"}</TableCell>
                  <TableCell>
                    {/* Status is the point of the table, so both states must read at a
                        glance: a tinted outline for success against the loud destructive pill. */}
                    <Badge
                      variant={log.status_code < 400 ? "outline" : "destructive"}
                      className={log.status_code < 400 ? "border-success/40 text-success" : undefined}
                    >
                      {log.status_code}
                    </Badge>
                  </TableCell>
                  <TableCell className="hidden text-right tabular-nums md:table-cell">
                    {formatLatency(log.latency_ms)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
      {isEmpty ? null : (
        <CardFooter className="border-t">
          <PaginationControls
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            hasMore={page.hasMore}
            onLimitChange={onLimitChange}
            onOffsetChange={onOffsetChange}
          />
        </CardFooter>
      )}
    </Card>
  );
}
