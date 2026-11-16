"""Encode a framing reference from an aligned full-resolution image."""

import argparse
import base64
import io

from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', help='Full-resolution camera frame')
    parser.add_argument('center_x', type=float)
    parser.add_argument('center_y', type=float)
    args = parser.parse_args()

    x, y = int(args.center_x), int(args.center_y)
    radius = 48
    with Image.open(args.image) as frame:
        if not (radius <= x < frame.width - radius and radius <= y < frame.height - radius):
            parser.error('gauge center is too close to the frame edge')
        crop = frame.crop((x-radius, y-radius, x+radius+1, y+radius+1)).convert('L')
        encoded = io.BytesIO()
        crop.save(encoded, format='PNG', optimize=True)
    print(base64.b64encode(encoded.getvalue()).decode('ascii'))


if __name__ == '__main__':
    main()
