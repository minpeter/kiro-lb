import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export function StatCardSkeleton() {
  return (
    <Card>
      <CardHeader className="gap-3 pb-2">
        <Skeleton className="h-3.5 w-24" />
        <Skeleton className="h-8 w-20" />
      </CardHeader>
    </Card>
  );
}

/** Placeholder that matches the real table's column count and row height. */
export function TableSkeleton({ rows = 4, columns = 5 }: { rows?: number; columns?: number }) {
  return (
    <div className="space-y-3" data-testid="table-skeleton">
      <div className="flex gap-4">
        {Array.from({ length: columns }).map((_, index) => (
          <Skeleton key={index} className="h-3.5 flex-1" />
        ))}
      </div>
      {Array.from({ length: rows }).map((_, row) => (
        <div key={row} className="flex gap-4">
          {Array.from({ length: columns }).map((_, column) => (
            <Skeleton key={column} className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

export function CardTableSkeleton({ rows, columns }: { rows?: number; columns?: number }) {
  return (
    <Card>
      <CardHeader className="gap-2">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3.5 w-72" />
      </CardHeader>
      <CardContent>
        <TableSkeleton rows={rows} columns={columns} />
      </CardContent>
    </Card>
  );
}

export function ChartSkeleton({ rows = 2 }: { rows?: number }) {
  return (
    <div className="space-y-5" data-testid="chart-skeleton">
      {Array.from({ length: rows }).map((_, row) => (
        <div key={row} className="space-y-1.5">
          <Skeleton className="h-3.5 w-40" />
          <Skeleton className="h-24 w-full" />
        </div>
      ))}
    </div>
  );
}

export function DashboardSkeleton() {
  return (
    <div className="space-y-6" data-testid="dashboard-skeleton">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <StatCardSkeleton key={index} />
        ))}
      </div>
      <CardTableSkeleton rows={3} columns={5} />
    </div>
  );
}
