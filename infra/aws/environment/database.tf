# Coordinator state on Amazon RDS for PostgreSQL (action R, phase 4).
#
# RDS creates and rotates the login itself (manage_master_user_password), so the
# password never passes through Terraform. The secret is encrypted with the secrets key
# and rotates every database.rotation_days; coordinators read it at connect
# time (DATABASE_SECRET_ID) and retry once with the new value after a rejected login.
# Connections must use TLS (rds.force_ssl), and the coordinator verifies the server
# certificate and hostname (sslmode=verify-full with the RDS CA bundle).

locals {
  postgres_major   = "16"
  database_name    = "optimizer"
  database_user    = "optimizer_admin"
  database_ca_path = "/etc/ssl/certs/rds-global-bundle.pem"
}

resource "aws_db_subnet_group" "state" {
  name       = "${local.name}-state"
  subnet_ids = local.subnet_ids.data
  tags       = { Name = "${local.name}-state" }
}

resource "aws_db_parameter_group" "state" {
  name_prefix = "${local.name}-pg${local.postgres_major}-"
  family      = "postgres${local.postgres_major}"
  description = "${local.name} coordinator state"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "ssl_min_protocol_version"
    value = "TLSv1.2"
  }
  parameter {
    # Matches the backend's 30 s statement timeout; catches clients that skip it.
    name  = "idle_in_transaction_session_timeout"
    value = "60000"
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "state" {
  identifier     = "${local.name}-state"
  engine         = "postgres"
  engine_version = local.postgres_major
  instance_class = var.database.instance_class
  multi_az       = var.database.multi_az

  db_name                       = local.database_name
  username                      = local.database_user
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.secrets.arn

  allocated_storage     = var.database.allocated_storage_gb
  max_allocated_storage = var.database.max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.data.arn

  db_subnet_group_name   = aws_db_subnet_group.state.name
  vpc_security_group_ids = [aws_security_group.data.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.state.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period    = var.database.backup_retention_days
  copy_tags_to_snapshot      = true
  delete_automated_backups   = !local.retain
  deletion_protection        = local.retain
  skip_final_snapshot        = !local.retain
  final_snapshot_identifier  = "${local.name}-state-final"
  auto_minor_version_upgrade = true
  apply_immediately          = !local.retain

  tags = { Name = "${local.name}-state", DataClass = "state" }
}

resource "aws_secretsmanager_secret_rotation" "database" {
  secret_id          = aws_db_instance.state.master_user_secret[0].secret_arn
  rotate_immediately = false
  rotation_rules {
    automatically_after_days = var.database.rotation_days
  }
}
