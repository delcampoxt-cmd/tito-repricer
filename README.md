# Tito Repricer — repricing cockpit for DelCampoXT

Scan your FBA inventory → see current price, Buy Box (and who holds it), fees, computed floor
(cost + all fees + $1.00 profit) and a proposed price per your rules → edit any price → Apply.
You stay in charge: nothing changes on Amazon without your click. Installs in DRY RUN mode.

## Install (one time)
1. In Terminal: `bash <this folder>/install.sh`
2. Open **Tito Repricer** from Applications.
3. Settings → **Import keys from FNSKU Labels** → Save. (Same Amazon app, zero new setup.)
4. **Scan now** → review → select → Apply. Turn off **Dry run** in Settings when ready.

## Rules (editable in Settings)
- Buy Box − $0.05, or Q4 override (until 2027-01-31): match the Buy Box, skipping Amazon-held ones.
- Floor: never below cost + all Amazon fees + $1.00 (computed per SKU from Amazon's fee estimates).
- Near-$10 items stay at $9.99 (FBA low-price fee tier).
- Cost per unit: manual > leading number in the SKU (6-dl67-26 → $6.00) > $1.85 default.
- You hold the Buy Box → hold. History logs every change with whether it won/held the Buy Box; one-click revert.

Data stays on this Mac in ~/.tito-repricer/ — keys are never uploaded anywhere.
