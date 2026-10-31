import { redirect } from "next/navigation";

/** Gastos fijos became the Presupuesto page; keep old links working. */
export default function FixedExpensesRedirect() {
  redirect("/budget");
}
