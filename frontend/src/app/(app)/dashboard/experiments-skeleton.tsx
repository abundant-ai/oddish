import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// Placeholder rows shown while the experiments list is (re)loading — e.g. when
// switching Org/Mine, selecting a member, changing status, or paging. Mirrors
// the real table's columns so the layout doesn't jump when data arrives.
export function ExperimentsSkeleton({ rows = 10 }: { rows?: number }) {
  return (
    <div
      className="max-h-[68vh] min-h-[560px] overflow-y-auto"
      aria-busy="true"
      aria-label="Loading experiments"
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Experiment</TableHead>
            <TableHead>Author</TableHead>
            <TableHead>Last run</TableHead>
            <TableHead>PR</TableHead>
            <TableHead>Tasks</TableHead>
            <TableHead>Trials</TableHead>
            <TableHead>Avg score</TableHead>
            <TableHead className="text-right">Last task</TableHead>
            <TableHead className="text-right">Delete</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: rows }).map((_, i) => (
            <TableRow key={i}>
              <TableCell>
                <Skeleton className="h-3.5 w-40" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-3.5 w-24" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-3.5 w-24" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-5 w-20 rounded-full" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-3.5 w-6" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-3.5 w-16" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-3.5 w-10" />
              </TableCell>
              <TableCell className="text-right">
                <Skeleton className="ml-auto h-3.5 w-20" />
              </TableCell>
              <TableCell className="text-right">
                <Skeleton className="ml-auto h-8 w-8 rounded-md" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
