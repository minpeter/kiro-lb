import { ScrollText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatLatency, formatTimestamp } from "../format";
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
                <TableHead className="text-right">Latency</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.logs.map((log, index) => (
                <TableRow key={`${log.created_at}-${page.offset + index}`}>
                  <TableCell>{formatTimestamp(log.created_at)}</TableCell>
                  <TableCell className="font-mono text-xs">{log.route}</TableCell>
                  <TableCell>{log.model ?? "—"}</TableCell>
                  <TableCell>
                    <Badge variant={log.status_code < 400 ? "secondary" : "destructive"}>{log.status_code}</Badge>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatLatency(log.latency_ms)}</TableCell>
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
