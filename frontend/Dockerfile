FROM node:20-slim

WORKDIR /workspace/frontend

ENV NODE_ENV=development

RUN chown node:node /workspace/frontend

COPY --chown=node:node package.json package-lock.json* ./

USER node

RUN npm install

COPY --chown=node:node . .

CMD ["sh", "-c", "rm -rf node_modules/.vite && npm run dev -- --host 0.0.0.0"]
