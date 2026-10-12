# Airflow image for the NBO pipeline: the official image + our providers, pinned by Airflow's constraint file
# (only versions Airflow tested together). CI builds the "test" stage to run the DAG tests in this exact image.
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.11

FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION} AS base
ARG AIRFLOW_VERSION
ARG PYTHON_VERSION
USER root
# git: Airflow pulls the DAGs from the repository's main branch (git DAG bundle)
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
USER airflow
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /requirements.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# test stage: same image + pytest (never shipped)
FROM base AS test
RUN pip install --no-cache-dir pytest==9.1.1

FROM base AS runtime