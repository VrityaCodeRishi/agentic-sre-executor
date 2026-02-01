project_id = "buoyant-episode-386713"
region     = "us-central1"

name_prefix = "gke"

enable_apis         = true
release_channel     = "REGULAR"
deletion_protection = false

vpc_cidr                = "10.10.0.0/16"
pods_secondary_cidr     = "10.20.0.0/16"
services_secondary_cidr = "10.30.0.0/20"

node_machine_type    = "e2-standard-4"
node_disk_type       = "pd-balanced"
node_disk_size_gb    = 100
node_min_count       = 1
node_max_count       = 3
node_preemptible     = false
node_service_account = null


