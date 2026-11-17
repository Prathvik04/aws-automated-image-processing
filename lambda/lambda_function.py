
import json
import os
import time
import logging
from io import BytesIO
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageOps, UnidentifiedImageError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS S3 client
s3 = boto3.client("s3")

# Configuration
DEST_BUCKET = os.environ["DEST_BUCKET"]
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_IMAGE_PIXELS = 25_000_000
MAX_DIMENSION = (1024, 1024)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def log_event(level, message, **details):
    """Write structured JSON logs to CloudWatch."""
    log_data = {
        "message": message,
        **details
    }
    getattr(logger, level)(json.dumps(log_data))


def process_image(source_bucket, source_key):

    start_time = time.time()

    log_event(
        "info",
        "Image processing started",
        source_bucket=source_bucket,
        source_key=source_key
    )

    try:
        # Download image metadata and validate file size
        response = s3.get_object(
            Bucket=source_bucket,
            Key=source_key
        )

        original_size = response["ContentLength"]

        if original_size <= 0:
            raise ValueError("Empty image file")

        if original_size > MAX_FILE_SIZE:
            raise ValueError("Image exceeds the 10 MB size limit")

        image_data = response["Body"].read()

        # Open and validate image
        with Image.open(BytesIO(image_data)) as image:

            image_format = image.format

            if image_format not in ALLOWED_FORMATS:
                raise ValueError(
                    f"Unsupported image format: {image_format}"
                )

            width, height = image.size

            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError(
                    "Image exceeds the 25 million pixel limit"
                )

            # Verify image integrity before processing
            image.verify()

        # Reopen image after verification
        with Image.open(BytesIO(image_data)) as image:

            image = ImageOps.exif_transpose(image)

            original_dimensions = image.size

            # Resize while preserving aspect ratio
            image.thumbnail(
                MAX_DIMENSION,
                Image.Resampling.LANCZOS
            )

            # Handle transparency using a white background
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                image = image.convert("RGBA")
                background = Image.new(
                    "RGB", image.size, (255, 255, 255)
                )
                background.paste(
                    image,
                    mask=image.getchannel("A")
                )
                image = background
            else:
                image = image.convert("RGB")

            # Compress as JPEG
            output_buffer = BytesIO()

            image.save(
                output_buffer,
                format="JPEG",
                quality=85,
                optimize=True
            )

        processed_data = output_buffer.getvalue()

        # Generate destination filename
        output_key = (
            os.path.splitext(source_key)[0] + "_resized.jpg"
        )

        # Upload processed image
        s3.put_object(
            Bucket=DEST_BUCKET,
            Key=output_key,
            Body=processed_data,
            ContentType="image/jpeg"
        )

        duration = round(time.time() - start_time, 3)

        log_event(
            "info",
            "Image processing completed",
            source_key=source_key,
            output_key=output_key,
            original_size_bytes=original_size,
            processed_size_bytes=len(processed_data),
            original_dimensions=original_dimensions,
            processed_dimensions=image.size,
            format=image_format,
            duration_seconds=duration
        )

        return output_key

    except (UnidentifiedImageError, OSError, ValueError) as error:

        log_event(
            "error",
            "Image validation or processing failed",
            source_key=source_key,
            error=str(error)
        )
        raise

    except ClientError as error:

        log_event(
            "error",
            "AWS S3 operation failed",
            source_key=source_key,
            error=str(error)
        )
        raise

    except Exception as error:

        logger.exception(
            "Unexpected image processing failure: %s",
            source_key
        )
        raise


def lambda_handler(event, context):

    log_event(
        "info",
        "Lambda invocation started",
        request_id=context.aws_request_id,
        record_count=len(event.get("Records", []))
    )

    results = []

    try:
        for record in event["Records"]:

            if record.get("eventSource") != "aws:s3":
                raise ValueError("Unsupported event source")

            source_bucket = record["s3"]["bucket"]["name"]

            source_key = unquote_plus(
                record["s3"]["object"]["key"]
            )

            output_key = process_image(
                source_bucket,
                source_key
            )

            results.append({
                "source_key": source_key,
                "output_key": output_key,
                "status": "success"
            })

        log_event(
            "info",
            "Lambda invocation completed",
            request_id=context.aws_request_id,
            processed_count=len(results)
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Image processing completed",
                "results": results
            })
        }

    except Exception:

        logger.exception(
            "Lambda invocation failed: %s",
            context.aws_request_id
        )
        raise
