import {
  ArrowLeftRight,
  Briefcase,
  Car,
  Circle,
  Coffee,
  Fuel,
  Gamepad2,
  Gift,
  GraduationCap,
  HeartPulse,
  House,
  PawPrint,
  PiggyBank,
  Pill,
  Plane,
  Receipt,
  Settings,
  Shirt,
  ShoppingCart,
  Smartphone,
  Tag,
  Utensils,
  Wifi,
  Zap,
  type LucideIcon,
  type LucideProps,
} from "lucide-react";
import { CATEGORY_ICON_NAMES, type CategoryIconName } from "@/lib/categories";

/** The lucide component and a Spanish label for every curated icon name. A
 *  full record, so a name added to CATEGORY_ICON_NAMES without an icon fails
 *  the type check. */
export const CATEGORY_ICONS: Record<
  CategoryIconName,
  { icon: LucideIcon; label: string }
> = {
  "shopping-cart": { icon: ShoppingCart, label: "Carro de compras" },
  car: { icon: Car, label: "Auto" },
  utensils: { icon: Utensils, label: "Cubiertos" },
  "gamepad-2": { icon: Gamepad2, label: "Videojuegos" },
  "heart-pulse": { icon: HeartPulse, label: "Salud" },
  settings: { icon: Settings, label: "Engranaje" },
  "arrow-left-right": { icon: ArrowLeftRight, label: "Transferencia" },
  circle: { icon: Circle, label: "Círculo" },
  house: { icon: House, label: "Casa" },
  zap: { icon: Zap, label: "Electricidad" },
  wifi: { icon: Wifi, label: "Internet" },
  smartphone: { icon: Smartphone, label: "Celular" },
  fuel: { icon: Fuel, label: "Combustible" },
  plane: { icon: Plane, label: "Viajes" },
  coffee: { icon: Coffee, label: "Café" },
  shirt: { icon: Shirt, label: "Ropa" },
  pill: { icon: Pill, label: "Farmacia" },
  "graduation-cap": { icon: GraduationCap, label: "Educación" },
  gift: { icon: Gift, label: "Regalos" },
  "paw-print": { icon: PawPrint, label: "Mascotas" },
  "piggy-bank": { icon: PiggyBank, label: "Ahorro" },
  receipt: { icon: Receipt, label: "Cuentas" },
  briefcase: { icon: Briefcase, label: "Trabajo" },
};

/** A stored icon name narrowed to the curated list, or null. */
export function toIconName(name: string | null): CategoryIconName | null {
  return name != null &&
    (CATEGORY_ICON_NAMES as readonly string[]).includes(name)
    ? (name as CategoryIconName)
    : null;
}

/** The icon for a stored name; a generic tag when it is missing or unknown. */
export function CategoryIcon({
  icon,
  ...props
}: { icon: string | null } & LucideProps) {
  const iconName = toIconName(icon);
  const Icon = iconName ? CATEGORY_ICONS[iconName].icon : Tag;
  return <Icon aria-hidden {...props} />;
}
