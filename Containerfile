FROM docker.io/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py share_server.py share_store.py user_store.py slide_io.py raster_slide.py slide_render.py slide_cache.py viewer_display.py tile_cache.py share_entry.sh ./
COPY pg_store.py share_store_pg.py user_store_pg.py share_shared.py annotation_access.py ./
# slide ID 化重构 P1-A/P1-B2：资产状态权威 + 安全路径派生（app/share_server 读取通道依赖）
COPY slide_store.py slide_storage.py slide_publish.py task_storage_lock.py ./
COPY platform_features.py settings_store.py budget_store.py auth_limit_store.py demo_store.py registration_store.py registration_mail_worker.py registration_antibot.py identity_store.py ./
COPY billing_pricing.py billing_store.py acquisition_store.py ./
COPY credit_store.py credit_payments.py ./
COPY spend_store.py site_stats_store.py ./
COPY public_site.py ./
COPY crop_guard.py upload_content.py upload_guard.py upload_task_store.py useradmin.py ./
COPY conversion_store.py conversion_worker.py slide_format_registry.py format_request_store.py format_request_http.py ./
COPY project_idempotency_store.py project_create_http.py conversion_http.py ./
COPY baidu_share_parser.py baidu_adapter.py baidu_import_store.py baidu_import_http.py baidu_ingest.py ./
COPY agreement_store.py research_consent_store.py research_store.py research_deletion_worker.py legal_render.py ./
COPY feedback_store.py ./
COPY cos_config.py cos_client.py cos_fast_relay.py cos_pool_store.py ingestion_store.py cos_ingest_worker.py upload_direct_class.py ./
COPY producer_import_store.py ./
# 0087 支付宝赞助订单：app.py 函数内延迟 import 的仓储/SDK 模块
COPY support_payments.py ./
COPY legal_docs/ legal_docs/
COPY kfb/ kfb/
COPY migrations/ migrations/
COPY scripts/ scripts/
COPY docker_entry.sh ./
RUN chmod +x docker_entry.sh
COPY templates/ templates/
COPY static/ static/
COPY plugins/ plugins/

ENV PORT=8000 \
    SHARE_PORT=38000 \
    UPLOAD_DIR=/data/uploads \
    SHARE_DATA_DIR=/data/share \
    PLUGIN_BUNDLES_DIR=/data/plugins \
    FORMAT_REQUEST_DIR=/data/format-requests \
    FORMAT_REQUEST_ADMIN_EMAIL=solarise94@gmail.com \
    FORMAT_REQUEST_WORKER=1 \
    TEST_APPLICATION_ADMIN_EMAIL=solarise94@gmail.com

EXPOSE 8000
CMD ["./docker_entry.sh"]
