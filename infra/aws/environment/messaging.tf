# Amazon MQ for RabbitMQ and the ElastiCache quarantine cache (action R, phase 4).
# Both live in the data tier, accept only TLS and are reachable only from the
# coordinator, scheduler and worker security groups.

locals {
  rabbitmq_version = "3.13" # Amazon MQ's RabbitMQ 3 line; runs on mq.t3 and mq.m5.
  rabbitmq_user    = "optimizer"
  valkey_version   = "8.0"
}

# --- Broker ------------------------------------------------------------------------
#
# Amazon MQ needs a first user and password when the broker is created, and Terraform
# 1.5 cannot keep a value out of state. The broker therefore starts with a random
# bootstrap password that exists only in (encrypted) state. `./project infra up` copies
# it into the rabbitmq-password secret and immediately rotates twice, which replaces
# that password through the RabbitMQ API, so the value in state no longer works.

resource "random_password" "broker_bootstrap" {
  length  = 48
  special = false
}

resource "aws_mq_broker" "main" {
  broker_name                = "${local.name}-broker"
  engine_type                = "RabbitMQ"
  engine_version             = local.rabbitmq_version
  auto_minor_version_upgrade = true # Required by Amazon MQ for RabbitMQ 3.13.
  host_instance_type         = var.broker.instance_type
  deployment_mode            = var.broker.deployment_mode
  publicly_accessible        = false
  subnet_ids = (
    var.broker.deployment_mode == "SINGLE_INSTANCE"
    ? [local.subnet_ids.data[0]]
    : local.subnet_ids.data
  )
  security_groups   = [aws_security_group.data.id]
  apply_immediately = !local.retain

  encryption_options {
    use_aws_owned_key = false
    kms_key_id        = aws_kms_key.data.arn
  }

  maintenance_window_start_time {
    day_of_week = "SUNDAY"
    time_of_day = "10:00"
    time_zone   = "UTC"
  }

  user {
    username = local.rabbitmq_user
    password = random_password.broker_bootstrap.result
  }

  lifecycle {
    # Users are managed by rotation after creation; Amazon MQ cannot update them.
    ignore_changes = [user]
  }

  tags = { Name = "${local.name}-broker", DataClass = "messages" }
}

locals {
  broker_host    = trimsuffix(trimprefix(aws_mq_broker.main.instances[0].endpoints[0], "amqps://"), ":5671")
  broker_api_url = aws_mq_broker.main.instances[0].console_url
}

# --- Cache -------------------------------------------------------------------------
#
# The cache only speeds up quarantine checks; durable quarantine evidence stays in the
# database, so losing the cache loses nothing. It is created without an AUTH token so
# no token enters Terraform state. `./project infra up` enables AUTH from the
# redis-auth-token secret (ROTATE, then SET), and rotations use ROTATE afterwards.

resource "aws_elasticache_subnet_group" "cache" {
  name       = "${local.name}-cache"
  subnet_ids = local.subnet_ids.data
}

resource "aws_elasticache_replication_group" "cache" {
  replication_group_id = "${local.name}-cache"
  description          = "${local.name} quarantine cache"
  engine               = "valkey"
  engine_version       = local.valkey_version
  parameter_group_name = "default.valkey8"
  node_type            = var.cache.node_type
  num_cache_clusters   = var.cache.nodes
  port                 = local.ports.redis

  automatic_failover_enabled = var.cache.nodes > 1
  multi_az_enabled           = var.cache.nodes > 1

  subnet_group_name  = aws_elasticache_subnet_group.cache.name
  security_group_ids = [aws_security_group.data.id]

  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn

  snapshot_retention_limit   = 0
  auto_minor_version_upgrade = true
  apply_immediately          = true
  maintenance_window         = "sun:11:00-sun:12:00"

  lifecycle {
    ignore_changes = [auth_token, auth_token_update_strategy]
  }

  tags = { Name = "${local.name}-cache", DataClass = "cache" }
}
