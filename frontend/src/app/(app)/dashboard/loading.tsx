import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export default function DashboardLoading() {
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="p-4">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="mt-3 h-16" />
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-4">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="mt-3 h-[360px]" />
        </CardContent>
      </Card>
    </div>
  );
}
