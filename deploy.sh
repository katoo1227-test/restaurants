#!/bin/bash

# AWSへのデプロイスクリプト
#
# Usage:
#   bash deploy.sh <env>
#
# Parameters:
#   <env> デプロイ先環境。dev or prodのみ受付
#
# Examples:
#   bash deploy.sh dev

# 引数が1つでないと終了
if [ $# -ne 1 ]; then
    echo "ex)bash deploy.sh dev"
    exit 1
fi

# 引数が "dev" または "prod" でない場合の処理
env=$1
if [ "$env" != "dev" ] && [ "$env" != "prod" ]; then
    echo "ex)bash deploy.sh dev"
    exit 1
fi

# テンプレートのベースパス
base_path="$(pwd)/template_base.yml"

# 完成後のテンプレートパス
yml_path="$(pwd)/template.yml"

# 「$file: ~~」をテンプレートにコピー
yq '(.. | select(has("$file"))) |= load(.$file) | .Resources = (.Resources[] as $item ireduce ({}; . * $item))' "$base_path" > "$yml_path"

# 各パラメータの値
environment_type=$env
task_name_register_pages="RegisterPages${env^}"
task_name_scraping_abstract="ScrapingAbstract${env^}"
task_name_scraping_detail="ScrapingDetail${env^}"

# ビルドとデプロイ
sam build --template-file ./template.yml
sam deploy \
    --config-env=$env \
    --parameter-overrides EnvironmentType=$env \
        TaskNameRegisterPages=$task_name_register_pages \
        TaskNameScrapingAbstract=$task_name_scraping_abstract \
        TaskNameScrapingDetail=$task_name_scraping_detail