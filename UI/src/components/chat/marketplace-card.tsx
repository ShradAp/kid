import Image from "next/image";
import { Star } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { MarketplaceProduct } from "@/lib/assistant-data";

export function MarketplaceCard({ product }: { product: MarketplaceProduct }) {
  return (
    <div className="flex gap-3 rounded-xl border border-white/10 bg-white/[0.03] p-3">
      <div className="relative size-20 shrink-0 overflow-hidden rounded-lg border border-white/10 bg-black/40">
        <Image
          src={product.image || "/placeholder.svg"}
          alt={product.title}
          fill
          className="object-cover"
        />
      </div>
      <div className="flex min-w-0 flex-1 flex-col justify-between gap-2">
        <div>
          <p className="truncate text-sm font-medium text-foreground">
            {product.title}
          </p>
          <div className="mt-1 flex items-center gap-2">
            <span className="text-sm font-semibold text-primary">
              {product.price}
            </span>
            <span className="flex items-center gap-0.5 text-xs text-amber-400">
              <Star className="size-3 fill-amber-400" />
              {product.rating}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {product.badges.map((badge) => (
            <Badge
              key={badge}
              variant="secondary"
              className="bg-white/[0.06] text-[10px] font-normal text-muted-foreground"
            >
              {badge}
            </Badge>
          ))}
        </div>
      </div>
    </div>
  );
}
