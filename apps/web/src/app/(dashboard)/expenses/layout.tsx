import { ExpensesTabs } from "@/components/expenses/ExpensesTabs";

export default function ExpensesLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-6">
      <div className="space-y-4">
        <h2 className="text-2xl font-bold">Gastos</h2>
        <ExpensesTabs />
      </div>
      {children}
    </div>
  );
}
