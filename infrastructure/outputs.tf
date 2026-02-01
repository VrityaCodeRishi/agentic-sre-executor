output "cluster_name" {
  description = "Name of the created GKE cluster."
  value       = google_container_cluster.gke.name
}

output "cluster_location" {
  description = "Region/location of the GKE cluster."
  value       = google_container_cluster.gke.location
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = google_container_cluster.gke.endpoint
}

output "cluster_ca_certificate" {
  description = "Base64-encoded public CA certificate for the cluster."
  value       = google_container_cluster.gke.master_auth[0].cluster_ca_certificate
  sensitive   = true
}

output "network" {
  description = "VPC network name."
  value       = google_compute_network.vpc.name
}

output "subnet" {
  description = "Subnetwork name."
  value       = google_compute_subnetwork.subnet.name
}


