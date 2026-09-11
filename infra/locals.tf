locals {
  name_prefix = "${var.project_name}-${var.environment}"

  # Repository root — this module lives in <repo>/infra.
  app_root = abspath("${path.module}/..")



  # ---------------------------------------------------------------------------
  # /opt/app/.env — non-secret half. Defaults mirror .env.example; the two
  # secrets are appended on the instance from SSM SecureStrings.
  # ---------------------------------------------------------------------------
  default_app_settings = {
    APP_NAME    = var.project_name
    APP_ENV     = var.environment
    APP_VERSION = "1.0.0"
    LOG_LEVEL   = "INFO"
    PORT        = "8080"
    APP_PORT    = "8080"

    DB_POOL_SIZE            = "5"
    DB_MAX_OVERFLOW         = "5"
    DB_TIMEOUT              = "10"
    DB_STATEMENT_TIMEOUT_MS = "60000"
    DB_AUTO_INIT            = "true"
    DB_SEED                 = "true"
    DB_SEED_USERS           = "25"

    HEALTH_CHECK_DB = "false"

    CPU_STRESS_MAX_DURATION    = "120"
    SLOW_QUERY_MAX_SECONDS     = "30"
    SLOW_QUERY_DEFAULT_SECONDS = "5"

    SIMULATE_DB_FAILURE      = "false"
    DB_FAILURE_MODE          = "connection_refused"
    DB_FAILURE_DELAY_SECONDS = "2"
    ENABLE_TEST_CONTROLS     = "true"

    TRAFFIC_GENERATOR_ENABLED  = "true"
    TRAFFIC_GENERATOR_RPS      = "2"
    TRAFFIC_GENERATOR_BASE_URL = "http://127.0.0.1:8080"

    GRAFANA_ADMIN_USER = var.grafana_admin_user
  }

  app_settings = merge(local.default_app_settings, var.app_settings)

  app_env_block = join("\n", [
    for k in sort(keys(local.app_settings)) : "${k}=${local.app_settings[k]}"
  ])

  ssm_prefix   = "/${var.project_name}/${var.environment}"
  ssm_db_url   = "${local.ssm_prefix}/DATABASE_URL"
  ssm_grafana  = "${local.ssm_prefix}/GRAFANA_ADMIN_PASSWORD"
  ssm_cw_agent = "${local.ssm_prefix}/cloudwatch-agent-config"

  metrics_namespace = "${var.project_name}/${var.environment}"

  log_group_system = "/${var.project_name}/${var.environment}/system"
  log_group_docker = "/${var.project_name}/${var.environment}/containers"
}
