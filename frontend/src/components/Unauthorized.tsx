import { ShieldAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export function Unauthorized() {
  return (
    <div className="flex items-center justify-center p-8">
      <Alert variant="destructive" className="max-w-md">
        <ShieldAlert className="h-4 w-4" />
        <AlertTitle>Access Denied</AlertTitle>
        <AlertDescription>
          You don't have permission to view this content. Contact your
          administrator if you believe this is an error.
        </AlertDescription>
      </Alert>
    </div>
  );
}
