import { ShieldAlert } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * Shown instead of every page when the app runs in production without
 * DASHBOARD_PASSWORD: the deployment fails closed rather than exposing data.
 */
export default function SetupRequiredPage() {
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <ShieldAlert className="h-8 w-8 text-destructive" />
          <CardTitle className="text-xl">Aplicación no configurada</CardTitle>
          <CardDescription>
            El dashboard no puede mostrarse porque no hay autenticación
            configurada.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted-foreground">
          <p>
            Define la variable de entorno <code>DASHBOARD_PASSWORD</code> y
            reinicia la aplicación para poder ingresar.
          </p>
          <p>
            Opcionalmente puedes ajustar <code>DASHBOARD_SESSION_MAX_AGE</code>{" "}
            para controlar cada cuánto se pide la contraseña de nuevo.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
