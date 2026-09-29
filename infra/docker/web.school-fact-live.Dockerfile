FROM node:22-alpine AS build

WORKDIR /build

ARG VITE_DINGTALK_CORP_ID
ARG VITE_DINGTALK_CLIENT_ID
ARG CAMPUS_AI_ICP_FILING_REQUIRED
ARG VITE_ICP_FILING_NUMBER
ENV VITE_DINGTALK_CORP_ID=${VITE_DINGTALK_CORP_ID}
ENV VITE_DINGTALK_CLIENT_ID=${VITE_DINGTALK_CLIENT_ID}
ENV VITE_ICP_FILING_NUMBER=${VITE_ICP_FILING_NUMBER}

RUN case "$VITE_DINGTALK_CORP_ID" in \
        ""|*[[:space:]]*) exit 1 ;; \
        *) ;; \
    esac \
    && case "$VITE_DINGTALK_CLIENT_ID" in \
        ""|*[[:space:]]*) exit 1 ;; \
        *) ;; \
    esac \
    && case "$CAMPUS_AI_ICP_FILING_REQUIRED" in \
        true) case "$VITE_ICP_FILING_NUMBER" in \
            ""|*[[:space:]]*) exit 1 ;; \
            *) ;; \
        esac ;; \
        false) test -z "$VITE_ICP_FILING_NUMBER" ;; \
        *) exit 1 ;; \
    esac

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./
RUN npm run build:dingtalk-school-facts

FROM nginx:1.27-alpine

COPY infra/nginx/school-facts-production.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /build/dist-dingtalk-school-facts /usr/share/nginx/html
RUN mv /usr/share/nginx/html/dingtalk-school-facts.html /usr/share/nginx/html/index.html \
    && rm -f /etc/nginx/conf.d/default.conf

EXPOSE 80 443
