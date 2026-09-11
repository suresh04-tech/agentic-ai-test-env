# Single-command deploy and destroy.
#
#   make deploy    -> init + apply, no prompts
#   make destroy   -> tear everything down, no prompts

TF ?= terraform

.PHONY: deploy destroy plan init fmt validate output url logs boot-logs status shell

deploy:
	$(TF) init -input=false -upgrade
	$(TF) apply -auto-approve -input=false
	@echo ""
	@echo "Deployed. It takes 3-5 more minutes for the instance to build the"
	@echo "images and pass the ALB health check."
	@$(TF) output -raw application_url; echo ""

destroy:
	$(TF) destroy -auto-approve -input=false

plan:
	$(TF) init -input=false
	$(TF) plan -input=false

init:
	$(TF) init -input=false -upgrade

fmt:
	$(TF) fmt -recursive

validate:
	$(TF) validate

output:
	$(TF) output

url:
	@$(TF) output -raw application_url; echo ""

status:
	@curl -fsS "$$($(TF) output -raw application_url)/health" && echo "" || echo "not healthy yet"

logs:
	aws logs tail "$$($(TF) output -raw container_log_group)" --follow --region "$$($(TF) output -raw region)"

boot-logs:
	aws logs tail "$$($(TF) output -raw system_log_group)" --follow --region "$$($(TF) output -raw region)"

shell:
	aws ssm start-session --target "$$($(TF) output -raw instance_id)" --region "$$($(TF) output -raw region)"
