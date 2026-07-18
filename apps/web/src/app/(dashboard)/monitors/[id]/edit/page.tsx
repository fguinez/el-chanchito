"use client";

import { useParams } from "next/navigation";
import { MonitorForm } from "@/components/monitors/MonitorForm";

export default function EditMonitorPage() {
  const { id } = useParams<{ id: string }>();
  return <MonitorForm monitorId={id} />;
}
