FROM node:24-alpine AS base
# corepack runs the pnpm version pinned in the root package.json
# `packageManager` (npm's `latest` tag now points at pnpm 12).
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
# The deps stage is the only install; `pnpm run` must not re-install.
ENV pnpm_config_verify_deps_before_run=false
WORKDIR /app
COPY package.json ./
RUN corepack enable && corepack install

FROM base AS deps
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/db-schema/package.json packages/db-schema/
COPY packages/product-model/package.json packages/product-model/
RUN pnpm install --frozen-lockfile

FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/web/node_modules ./apps/web/node_modules
COPY . .
RUN NEXT_STANDALONE=1 pnpm --filter @chanchito/web build

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/apps/web/public ./apps/web/public
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/apps/web/.next/static ./apps/web/.next/static
USER nextjs
EXPOSE 3000
ENV PORT=3000
CMD ["node", "apps/web/server.js"]
