FROM public.ecr.aws/lambda/python:3.11
COPY services/aws_publishing/pyproject.toml ${LAMBDA_TASK_ROOT}/service/
COPY services/aws_publishing/aws_publishing/ ${LAMBDA_TASK_ROOT}/service/aws_publishing/
RUN pip install --no-cache-dir ${LAMBDA_TASK_ROOT}/service
# Copy only stdlib contracts and locked policy. Connector/scientific code stays private.
COPY src/aws_mesouq/__init__.py src/aws_mesouq/contracts.py src/aws_mesouq/release.py src/aws_mesouq/benchmark.py ${LAMBDA_TASK_ROOT}/aws_mesouq/
COPY src/aws_mesouq/presets/ ${LAMBDA_TASK_ROOT}/aws_mesouq/presets/
CMD ["aws_publishing.handler.handler"]
