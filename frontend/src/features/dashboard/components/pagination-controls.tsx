import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];

export type PaginationControlsProps = {
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  onLimitChange: (limit: number) => void;
  onOffsetChange: (offset: number) => void;
};

export function PaginationControls({
  total,
  limit,
  offset,
  hasMore,
  onLimitChange,
  onOffsetChange,
}: PaginationControlsProps) {
  const lastPageOffset = total > 0 ? Math.max(0, Math.ceil(total / limit) - 1) * limit : 0;
  const rangeStart = total > 0 ? offset + 1 : 0;
  const rangeEnd = Math.min(offset + limit, total);

  return (
    <div className="flex flex-wrap items-center justify-end gap-2 text-xs">
      <span className="text-muted-foreground">Rows</span>
      <Select value={String(limit)} onValueChange={(value) => onLimitChange(Number(value))}>
        <SelectTrigger size="sm" className="w-20" aria-label="Rows per page">
          <SelectValue />
        </SelectTrigger>
        <SelectContent align="end">
          {PAGE_SIZE_OPTIONS.map((size) => (
            <SelectItem key={size} value={String(size)}>
              {size}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <span className="tabular-nums text-muted-foreground">
        {rangeStart}–{rangeEnd} of {total.toLocaleString()}
      </span>

      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        disabled={offset <= 0}
        onClick={() => onOffsetChange(0)}
        aria-label="First page"
      >
        <ChevronsLeft />
      </Button>
      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        disabled={offset <= 0}
        onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        aria-label="Previous page"
      >
        <ChevronLeft />
      </Button>
      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        disabled={!hasMore}
        onClick={() => onOffsetChange(offset + limit)}
        aria-label="Next page"
      >
        <ChevronRight />
      </Button>
      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        disabled={!hasMore}
        onClick={() => onOffsetChange(lastPageOffset)}
        aria-label="Last page"
      >
        <ChevronsRight />
      </Button>
    </div>
  );
}
