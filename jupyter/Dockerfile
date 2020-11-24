FROM jupyter/datascience-notebook:python-3.7.6

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends apt-utils && \
    apt-get install -y libpq-dev
    
RUN pip install psycopg2 && \
    fix-permissions $CONDA_DIR && \
    fix-permissions /home/$NB_USER